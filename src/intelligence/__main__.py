"""Inspect, synchronize or backfill publication images."""

from __future__ import annotations


import argparse
import json
import logging
import sys

import requests

from .config import Settings
from .backfill import backfill
from .odoo import OdooClient
from .sources import EBC_HOST, rss_publisher
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
    concluidas = stats.get('fontes_concluidas', 0)
    falhas = stats.get('fontes_com_erro', 0)
    tentadas = concluidas + falhas
    return tentadas > 0 and concluidas == 0 and falhas > 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Odoo data sync')
    parser.add_argument(
        'command', choices=['inspect', 'preview', 'apply', 'images-preview', 'images-apply'],
        nargs='?', default='preview',
    )
    parser.add_argument('--limit', type=int, default=100, help='Maximum records per image backfill')
    parser.add_argument('--after-id', type=int, default=0, help='Resume image backfill after this record ID')
    parser.add_argument('--summary-only', action='store_true', help='Omit instance details from output')
    args = parser.parse_args(argv)
    stage = 'settings'
    try:
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
                    if rss_publisher(source['x_studio_url']) not in (None, EBC_HOST)
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
                            'approved_feed': url if rss_publisher(url) else None,
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
        )
        stats = synchronizer.run()
        output: dict[str, object] = {'mode': args.command, 'counts': stats}
        if not args.summary_only:
            output['sources'] = synchronizer.source_audit
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if _todas_as_fontes_falharam(stats) or stats.get('status_erros', 0) > 0:
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
