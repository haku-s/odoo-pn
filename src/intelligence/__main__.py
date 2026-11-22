"""CLI: inspect (read-only), preview and apply."""

from __future__ import annotations

import argparse
import json
import logging
import sys

import requests

from .config import Settings
from .odoo import OdooClient
from .sync import Synchronizer

USER_AGENT = 'odoo-data-sync/1.0'


def _session(cfg: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        'Accept': 'application/json, application/rss+xml, application/xml',
        'User-Agent': USER_AGENT,
    })
    return session


def _todas_as_fontes_falharam(stats: dict) -> bool:
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
        'command', choices=['inspect', 'preview', 'apply'], nargs='?', default='preview',
    )
    args = parser.parse_args(argv)
    stage = 'settings'
    try:
        cfg = Settings.from_env()
        stage = 'Odoo metadata'
        odoo = OdooClient(cfg)
        if args.command == 'inspect':
            odoo.check(write=True)
            metadata = {
                'fontes': len(odoo.fontes()),
                'assuntos': len(odoo.assuntos()),
                'metodos': list(odoo.selection('x_fontes', 'x_studio_metodo_de_coleta')),
                'tipos': list(odoo.selection('x_publicacoes', 'x_studio_tipo_de_publicacao')),
                'areas': list(odoo.selection('x_assuntos', 'x_studio_area')),
            }
            print(json.dumps({'mode': 'inspect', 'status': 'passed', **metadata}, ensure_ascii=False, sort_keys=True))
            return 0

        stage = 'collection'
        stats = Synchronizer(cfg, odoo, _session(cfg), apply=(args.command == 'apply')).run()
        print(json.dumps(
            {'mode': args.command, 'counts': stats},
            ensure_ascii=False, sort_keys=True,
        ))
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
