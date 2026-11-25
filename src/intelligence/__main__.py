"""Inspect, synchronize or backfill publication images."""

from __future__ import annotations


import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import requests

from .config import Settings
from .backfill import backfill
from .odoo import OdooClient
from .sources import EBC_HOST, news_publisher
from .sync import Synchronizer

USER_AGENT = 'odoo-data-sync/1.0'


def _session(cfg: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        'Accept': 'application/json, application/rss+xml, application/xml',
        'User-Agent': USER_AGENT,
    })
    return session


def _todas_as_fontes_falharam(stats: dict[str, int]) -> bool:
    """True when at least one source was attempted and none completed.

    A source only counts as completed after its collection *and*
    synchronisation finished successfully; a download that later fails is not
    a completed source.
    """
    concluidas = stats.get('fontes_concluidas', 0) + stats.get('fontes_parciais', 0)
    falhas = stats.get('fontes_com_erro', 0)
    tentadas = concluidas + falhas
    return tentadas > 0 and concluidas == 0 and falhas > 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Odoo data sync')
    parser.add_argument(
        'command', choices=['inspect', 'preview', 'apply', 'images-preview', 'images-apply', 'diagnose-news',
                            'funding-preview', 'funding-check', 'funding-sync', 'science-preview'],
        nargs='?', default='preview',
    )
    parser.add_argument('--limit', type=int, default=100, help='Maximum records per image backfill')
    parser.add_argument('--after-id', type=int, default=0, help='Resume image backfill after this record ID')
    parser.add_argument('--summary-only', action='store_true', help='Omit instance details from output')
    parser.add_argument('--preview-source', help='Test one registered source without changing its status')
    parser.add_argument('--news-audit-file', help='Write a bounded local rejection sample to a new private file')
    parser.add_argument('--funding-baseline', type=Path, help='Compare a previous funding preview snapshot')
    parser.add_argument('--funding-odoo', action='store_true', help='Use the Odoo funding baseline in preview')
    parser.add_argument('--science-baseline', type=Path, help='Read a previous private science snapshot')
    parser.add_argument('--science-output', type=Path, help='Save a science snapshot in a new private file')
    parser.add_argument('--science-odoo', action='store_true', help='Inspect the Odoo publication schema in read-only mode')
    parser.add_argument('--from-date', type=date.fromisoformat, help='First replay date (YYYY-MM-DD)')
    parser.add_argument('--through-date', type=date.fromisoformat, help='Last replay date (YYYY-MM-DD, inclusive)')
    args = parser.parse_args(argv)
    if args.preview_source and args.command != 'preview':
        parser.error('--preview-source requires preview mode')
    if args.news_audit_file and args.command != 'diagnose-news':
        parser.error('--news-audit-file requires diagnose-news mode')
    if (args.funding_baseline or args.funding_odoo) and args.command != 'funding-preview':
        parser.error('Funding baseline options require funding-preview mode')
    if args.funding_baseline and args.funding_odoo:
        parser.error('Select one funding baseline')
    if (args.science_baseline or args.science_output or args.science_odoo) and args.command != 'science-preview':
        parser.error('Science options require science-preview mode')
    period = None
    if args.from_date or args.through_date:
        if not args.from_date or not args.through_date or args.command not in (
            'preview', 'apply', 'diagnose-news', 'science-preview',
        ):
            parser.error('Both replay dates are required in a compatible mode')
        period = (args.from_date, args.through_date)
    stage = 'settings'
    try:
        if args.command == 'science-preview':
            from datetime import datetime, timezone

            from .odoo import PUBLICACOES, PUBLICACOES_FIELDS
            from .science import default_start, load_snapshot, preview

            stage = 'science collection'
            today = datetime.now(timezone.utc).date()
            start, end = period or (default_start(today), today)
            previous = load_snapshot(args.science_baseline) if args.science_baseline else []
            result = preview(start, end, previous)
            inspection: dict[str, object] = {}
            publication_fields: dict[str, object] = {}
            if args.science_odoo:
                try:
                    odoo = OdooClient(Settings.from_env())
                    subjects = odoo.assuntos()
                    metadata = odoo.fields(PUBLICACOES)
                    publication_fields = {name: meta.get('type') for name, meta in metadata.items()
                                          if isinstance(meta, dict) and name.startswith('x_')
                                          and name not in PUBLICACOES_FIELDS}
                    inspection = {
                        'status': 'read', 'active_subject_count': len(subjects),
                        'publication_types': list(odoo.selection(PUBLICACOES, 'x_studio_tipo_de_publicacao')),
                        'additional_publication_fields': len(publication_fields),
                    }
                except (ValueError, RuntimeError, requests.RequestException):
                    inspection = {'status': 'unavailable'}
                result['odoo'] = inspection
            if args.science_output:
                stage = 'science snapshot'
                with open(args.science_output, 'x', encoding='utf-8',
                          opener=lambda path, flags: os.open(path, flags, 0o600)) as report:
                    payload = result | {'odoo': inspection | {'publication_fields': publication_fields}} if inspection else result
                    json.dump(payload, report, ensure_ascii=False, indent=2)
            if args.summary_only:
                result.pop('records')
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return int(result['status'] == 'failed')
        if args.command.startswith('funding-'):
            from .funding import load_baseline, opportunity_from_dict
            from .funding_odoo import FundingStore
            from .funding_sources import preview

            stage = 'funding preflight'
            store = None
            previous = []
            if args.command != 'funding-preview' or args.funding_odoo:
                store = FundingStore(OdooClient(Settings.from_env()))
                store.check(write=args.command != 'funding-preview')
                previous = store.load()
            elif args.funding_baseline:
                previous = load_baseline(args.funding_baseline)
            if args.command == 'funding-check':
                print(json.dumps({'mode': args.command, 'status': 'passed', 'persisted': len(previous)}))
                return 0
            stage = 'funding collection'
            result = preview(previous)
            if args.command == 'funding-sync':
                if store is None:
                    raise RuntimeError('Funding store is unavailable')
                stage = 'funding persistence'
                stored = 0
                failures = 0
                for row in result['opportunities']:
                    try:
                        store.append(opportunity_from_dict(row))
                        stored += 1
                    except (RuntimeError, ValueError) as exc:
                        failures += 1
                        print(f'Funding revision could not be confirmed: {exc}', file=sys.stderr)
                result.update(mode=args.command, stored=stored, persistence_errors=failures)
            if args.summary_only:
                result.update(opportunity_count=len(result.pop('opportunities')), change_count=len(result.pop('changes')))
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return int(result['status'] == 'failed' or bool(result.get('persistence_errors')))
        cfg = Settings.from_env()
        stage = 'Odoo metadata'
        odoo = OdooClient(cfg)
        if args.command == 'inspect':
            odoo.check()
            sources = odoo.fontes(include_archived=True)
            avatar = odoo.avatar_info()
            metadata: dict[str, object] = {
                'fontes': sum(source['x_active'] for source in sources),
                'assuntos': len(odoo.assuntos()),
                'new_feed_endpoints_present': len({
                    source['x_studio_url'] for source in sources
                    if news_publisher(source['x_studio_url']) not in (None, EBC_HOST)
                }),
                'avatar': {
                    key: avatar[key]
                    for key in ('status', 'type', 'widget', 'writable', 'empty', 'populated',
                                'empty_representation', 'populated_representation')
                    if key in avatar
                },
            }
            if not args.summary_only:
                metadata.update({
                    'metodos': list(odoo.selection('x_fontes', 'x_studio_metodo_de_coleta')),
                    'tipos': list(odoo.selection('x_publicacoes', 'x_studio_tipo_de_publicacao')),
                    'areas': list(odoo.selection('x_assuntos', 'x_studio_area')),
                    'avatar': avatar,
                    'sources': [
                        {
                            'code': source['x_studio_codigo_da_fonte_1'],
                            'active': source['x_active'],
                            'enabled': source['x_studio_coleta_habilitada'],
                            'method': source['x_studio_metodo_de_coleta'],
                            'approved_feed': url if news_publisher(url) else None,
                        }
                        for source in sources
                        for url in [source['x_studio_url']]
                    ],
                })
            print(json.dumps({'mode': 'inspect', 'status': 'passed', **metadata}, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command in ('images-preview', 'images-apply'):
            stage = 'image backfill'
            result = backfill(
                cfg, odoo, _session(cfg), apply=args.command == 'images-apply',
                limit=args.limit, after_id=args.after_id,
            )
            if args.summary_only:
                result.pop('sources', None)
            print(json.dumps({'mode': args.command, **result}, ensure_ascii=False, sort_keys=True))
            return int(result['failed'] > 0)

        stage = 'collection'
        synchronizer = Synchronizer(
            cfg, odoo, _session(cfg), apply=(args.command == 'apply'),
            show_classification_details=not args.summary_only,
            capture_news_audit=bool(args.news_audit_file),
        )
        diagnose = args.command == 'diagnose-news'
        if diagnose:
            stats = synchronizer.run(period=period, diagnose_news=True)
            if args.news_audit_file:
                stage = 'news audit file'
                with open(args.news_audit_file, 'x', encoding='utf-8',
                          opener=lambda path, flags: os.open(path, flags, 0o600)) as report:
                    json.dump(synchronizer.news_rejections, report, ensure_ascii=False, indent=2)
        else:
            stats = synchronizer.run(preview_source=args.preview_source, period=period)
        fatal = _todas_as_fontes_falharam(stats) or stats.get('status_erros', 0) > 0
        targeted = stats.get('fontes_concluidas', 0) + stats.get('fontes_parciais', 0)
        partial = any(stats.get(key, 0) for key in ('fontes_com_erro', 'fontes_parciais', 'fontes_nao_implementadas'))
        status = 'failed' if fatal or (args.preview_source and targeted != 1) else 'partial' if partial else 'success'
        output: dict[str, object] = {
            'mode': args.command, 'status': status, 'counts': stats, 'sources': synchronizer.source_audit,
            'source_states': synchronizer.source_states, 'source_errors': synchronizer.source_errors,
        }
        if diagnose:
            output['source_registration'] = synchronizer.source_registration
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if args.preview_source and targeted != 1:
            print('Requested source did not complete the preview', file=sys.stderr)
            return 1
        if fatal:
            print('Every attempted source failed or the status update failed', file=sys.stderr)
            return 1
        return 0
    except (OSError, UnicodeError):
        print(f'Error in {stage}: local input or output failed', file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print(f'Error in {stage}: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f'Interrupted during {stage}', file=sys.stderr)
        return 130


if __name__ == '__main__':
    logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)
    sys.exit(main())
