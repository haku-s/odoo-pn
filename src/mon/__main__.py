"""CLI: inspect (read-only), preview and apply."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from .config import Filters, Settings
from .odoo import OdooClient
from .pn import PNCPClient
from .sync import Synchronizer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='odoo')
    parser.add_argument(
        'command', choices=['inspect', 'preview', 'apply'],
        nargs='?', default='preview',
    )
    args = parser.parse_args(argv)
    stage = 'settings'
    try:
        cfg = Settings.from_env()
        if args.command == 'inspect':
            stage = 'Odoo preflight'
            client = OdooClient(cfg)
            inventory = []
            status = 'failed'
            try:
                client.check_fields(write=True, report=inventory)
                status = 'passed'
            finally:
                fields = Counter(entry['status'] for entry in inventory)
                print(json.dumps({
                    'mode': 'inspect', 'status': status,
                    'counts': {
                        'fields_checked': len(inventory),
                        'fields_validated': fields['validated'],
                        'fields_unresolved': fields['unresolved'],
                        'fields_ambiguous': fields['ambiguous'],
                        'fields_incompatible': fields['incompatible'],
                        'fields_excluded': fields['excluded'],
                        'fields_write_enabled': sum(entry['write_enabled'] for entry in inventory),
                    },
                }, sort_keys=True))
            return 0

        stage = 'filters'
        filters = Filters.load()
        stage = 'Odoo configuration'
        if args.command == 'apply':
            cfg.require_odoo(write=True)
        odoo = OdooClient(cfg) if args.command == 'apply' or cfg.odoo_api_key else None
        stage = 'collection'
        stats = Synchronizer(
            cfg, filters, PNCPClient(), odoo,
            apply=(args.command == 'apply'),
        ).run()
        print(json.dumps(
            {'mode': args.command, 'counts': stats},
            ensure_ascii=False, sort_keys=True,
        ))
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
    sys.exit(main())
