"""CLI: inspect (read-only), preview and apply."""

from __future__ import annotations

import argparse
import json
import sys

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
    try:
        cfg = Settings.from_env()
        if args.command == 'inspect':
            client = OdooClient(cfg)
            inventory = []
            try:
                client.check_fields(write=True, report=inventory)
            finally:
                if inventory:
                    print(json.dumps({'fields': inventory}, ensure_ascii=False, sort_keys=True))
            return 0

        filters = Filters.load()
        if args.command == 'apply':
            cfg.require_odoo(write=True)
        odoo = OdooClient(cfg) if args.command == 'apply' or cfg.odoo_api_key else None
        stats = Synchronizer(
            cfg, filters, PNCPClient(), odoo,
            apply=(args.command == 'apply'),
        ).run()
        print(json.dumps(
            {'mode': args.command, 'counts': stats},
            ensure_ascii=False, sort_keys=True,
        ))
        return 0
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
