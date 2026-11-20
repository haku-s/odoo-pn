"""CLI: inspect (read-only), preview and apply."""

from __future__ import annotations

import argparse
import json
import os
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
            cfg.require_odoo()
            client = OdooClient(cfg)
            fields = client.check_fields(write=False)
            client.project([])
            for model, names in fields.items():
                report = {'model': model, 'field_count': len(names)}
                if os.getenv('ODOO_INSPECT_FIELDS') == '1':
                    report['fields'] = {
                        name: meta.get('type')
                        for name, meta in names.items()
                        if name.startswith('x_') or name in (
                            'name', 'description', 'project_id', 'stage_id',
                        )
                    }
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        filters = Filters.load()
        if args.command == 'apply':
            cfg.require_odoo(write=True)
        odoo = OdooClient(cfg) if args.command == 'apply' else None
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
