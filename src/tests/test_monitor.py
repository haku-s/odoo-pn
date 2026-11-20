import json
import os
import unittest

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mon.checkpoint import (
    days_to_process,
    parse_checkpoint,
    serialize_checkpoint,
)
from mon.config import Filters, Settings
from mon.normalizer import (
    clean_money,
    normalize,
    odoo_datetime,
)
from mon.odoo import OdooClient
from mon.pn import PNCPClient
from mon.relevance import is_relevant
from mon.sync import Synchronizer


FIXTURE = Path(__file__).parent / 'fixtures' / 'hiring.json'
RECORD = json.loads(FIXTURE.read_text(encoding='utf-8'))
FILT = Filters((6,), ('software',), (), ())
TODAY = date(2026, 11, 20)


class PNCPTests(unittest.TestCase):
    def test_normalize_preserves_values(self):
        d = normalize(RECORD)

        self.assertEqual(d['valor_estimado'], 379000.0)
        self.assertEqual(
            d['pncp_id'],
            RECORD['numeroControlePNCP'],
        )
        self.assertEqual(
            d['data_encerramento'],
            '2026-11-27 13:00:00',
        )
        self.assertEqual(len(d['assinatura']), 64)

    def test_no_value_not_zero(self):
        rec = {
            **RECORD,
            'valorTotalEstimado': None,
        }

        self.assertIsNone(
            normalize(rec)['valor_estimado']
        )

    def test_invalid_identifier(self):
        with self.assertRaises(ValueError):
            normalize({
                **RECORD,
                'numeroControlePNCP': 'invalid',
            })

    def test_money_zero(self):
        self.assertEqual(clean_money(0), 0.0)

    def test_datetime_from_brasilia(self):
        self.assertEqual(
            odoo_datetime('2026-11-27T10:00:00'),
            '2026-11-27 13:00:00',
        )

    def test_relevance_ignores_accents(self):
        f = Filters(
            (6,),
            ('softwáre',),
            (),
            (),
        )

        self.assertTrue(is_relevant(RECORD, f))

    def test_exclusion(self):
        f = Filters(
            (6,),
            ('software',),
            ('software',),
            (),
        )

        self.assertFalse(is_relevant(RECORD, f))

    def test_uf_filter(self):
        self.assertFalse(
            is_relevant(
                RECORD,
                Filters((6,), ('software',), (), ('RJ',)),
            )
        )

    def test_filter_secret_overrides_public_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'filters.json'

            path.write_text(
                json.dumps({
                    'modalidades': [6],
                    'include_any': [],
                    'exclude_any': [],
                    'ufs': [],
                }),
                encoding='utf-8',
            )

            with patch.dict(
                os.environ,
                {
                    'PNCP_FILTERS_JSON': json.dumps({
                        'modalidades': [6],
                        'include_any': ['software'],
                        'exclude_any': [],
                        'ufs': [],
                    }),
                },
            ):
                self.assertEqual(
                    Filters.load(path).include_any,
                    ('software',),
                )

    def test_field_map_cannot_overwrite_name(self):
        with patch.dict(
            os.environ,
            {'ODOO_FIELD_MAP': '{"objeto":"name"}'},
        ):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_filter_requires_keywords(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'filters.json'

            path.write_text(
                json.dumps({
                    'modalidades': [6],
                    'include_any': [],
                    'exclude_any': [],
                    'ufs': [],
                }),
                encoding='utf-8',
            )

            with self.assertRaises(ValueError):
                Filters.load(path)

    def test_date_overlap(self):
        last = TODAY - timedelta(days=1)

        days = list(
            days_to_process(
                TODAY - timedelta(days=10),
                last,
                TODAY,
                2,
                7,
            )
        )

        self.assertEqual(
            days[0],
            TODAY - timedelta(days=2),
        )
        self.assertEqual(days[-1], TODAY)

    def test_backlog_limit_no_skip(self):
        last = TODAY - timedelta(days=10)

        days = list(
            days_to_process(
                TODAY - timedelta(days=30),
                last,
                TODAY,
                2,
                3,
            )
        )

        self.assertEqual(
            days,
            [
                TODAY - timedelta(days=11),
                TODAY - timedelta(days=10),
                TODAY - timedelta(days=9),
            ],
        )

    def test_checkpoint_roundtrip(self):
        self.assertEqual(
            parse_checkpoint(
                serialize_checkpoint(TODAY)
            ),
            TODAY,
        )

    def test_checkpoint_rejects_unknown_version(self):
        with self.assertRaises(ValueError):
            parse_checkpoint(
                '{"version":2,"last_complete_day":"2026-11-19"}'
            )


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._data


class FakePNCPSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = iter(responses)
        self.requests = []

    def get(self, url, params, timeout):
        self.requests.append(
            (url, dict(params))
        )
        return next(self.responses)


class PaginationTests(unittest.TestCase):
    def test_204_empty(self):
        session = FakePNCPSession([
            FakeResponse(204),
        ])

        self.assertEqual(
            list(
                PNCPClient(session).list_day(
                    'publicacao',
                    TODAY,
                    6,
                    50,
                    30,
                )
            ),
            [],
        )

    def test_two_pages(self):
        session = FakePNCPSession([
            FakeResponse(
                data={
                    'data': [RECORD],
                    'totalPaginas': 2,
                }
            ),
            FakeResponse(
                data={
                    'data': [RECORD],
                    'totalPaginas': 2,
                }
            ),
        ])

        items = list(
            PNCPClient(session).list_day(
                'atualizacao',
                TODAY,
                6,
                50,
                30,
                uf='SP',
            )
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(
            session.requests[1][1]['pagina'],
            2,
        )
        self.assertEqual(
            session.requests[1][1]['uf'],
            'SP',
        )

    def test_exceeded_page_cap_fails(self):
        session = FakePNCPSession([
            FakeResponse(
                data={
                    'data': [],
                    'totalPaginas': 301,
                }
            ),
        ])

        with self.assertRaises(RuntimeError):
            list(
                PNCPClient(session).list_day(
                    'publicacao',
                    TODAY,
                    6,
                    50,
                    300,
                )
            )

    def test_bad_shape_fails(self):
        session = FakePNCPSession([
            FakeResponse(
                data={'other': []},
            ),
        ])

        with self.assertRaises(RuntimeError):
            list(
                PNCPClient(session).list_day(
                    'publicacao',
                    TODAY,
                    6,
                    50,
                    30,
                )
            )


class FakeOdooSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(
        self,
        url,
        headers,
        json,
        timeout,
        allow_redirects,
    ):
        self.calls.append(
            (url, json, headers)
        )
        return next(self.responses)


def cfg(**overrides):
    defaults = dict(
        today=TODAY,
        initial_date=TODAY - timedelta(days=1),
        overlap_days=2,
        max_days=7,
        page_size=50,
        max_pages=300,
        odoo_url='https://example.odoo.com',
        odoo_database='',
        odoo_api_key='TEST_SECRET_NOT_REAL',
        project_id=11,
        pncp_field='x_pncp_id',
        checkpoint_field='x_pncp_checkpoint',
        stage_id=None,
        field_map={'objeto': 'x_objeto'},
    )

    defaults.update(overrides)

    return SimpleNamespace(
        **defaults,
        require_odoo=lambda write=False: None,
    )


class OdooTests(unittest.TestCase):
    def test_create_uses_vals_list_not_vals(self):
        session = FakeOdooSession([
            FakeResponse(data=[99]),
        ])

        client = OdooClient(cfg(), session)
        client.create_task({'name': 'Sample'})

        self.assertEqual(
            session.calls[0][1],
            {'vals_list': [{'name': 'Sample'}]},
        )

        self.assertNotIn(
            'TEST_SECRET_NOT_REAL',
            str(session.calls[0][1]),
        )

    def test_write_uses_ids(self):
        session = FakeOdooSession([
            FakeResponse(data=True),
        ])

        client = OdooClient(cfg(), session)
        client.update_task(
            15,
            {'x_objeto': 'updated'},
        )

        self.assertEqual(
            session.calls[0][1]['ids'],
            [15],
        )
        self.assertEqual(
            session.calls[0][1]['vals']['x_objeto'],
            'updated',
        )

    def test_check_fields_wrong_type_fails(self):
        session = FakeOdooSession([
            FakeResponse(
                data={
                    'x_pncp_id': {'type': 'char'},
                    'project_id': {'type': 'many2one'},
                    'name': {'type': 'char'},
                    'x_objeto': {'type': 'text'},
                }
            ),
            FakeResponse(
                data={
                    'x_pncp_checkpoint': {
                        'type': 'char',
                    },
                }
            ),
        ])

        with self.assertRaises(ValueError):
            OdooClient(
                cfg(),
                session,
            ).check_fields(write=True)

    def test_dedup_fails_if_odoo_duplicates(self):
        session = FakeOdooSession([
            FakeResponse(
                data=[
                    {
                        'id': 1,
                        'x_pncp_id': RECORD[
                            'numeroControlePNCP'
                        ],
                    },
                    {
                        'id': 2,
                        'x_pncp_id': RECORD[
                            'numeroControlePNCP'
                        ],
                    },
                ]
            ),
        ])

        with self.assertRaises(RuntimeError):
            OdooClient(
                cfg(),
                session,
            ).tasks([
                RECORD['numeroControlePNCP'],
            ])


class FakeSource:
    def list_day(
        self,
        kind,
        day,
        modality,
        page_size,
        max_pages,
        uf=None,
    ):
        return (
            [RECORD]
            if kind == 'publicacao'
            else []
        )


class FakeOdoo:
    def __init__(self):
        self.records = {}
        self.created = []
        self.updated = []
        self.checkpoints = []

    def check_fields(self, write=False):
        return {}

    def has_unlinked_tasks(self):
        return False

    def project(self, fields):
        return {
            fields[0]: (
                self.checkpoints[-1]
                if self.checkpoints
                else False
            )
        }

    def tasks(self, ids):
        return {
            i: self.records[i]
            for i in ids
            if i in self.records
        }

    def create_task(self, vals):
        self.created.append(vals)

        self.records[vals['x_pncp_id']] = {
            'id': len(self.created),
            'x_pncp_id': vals['x_pncp_id'],
            'x_objeto': vals['x_objeto'],
        }

    def update_task(self, task_id, vals):
        self.updated.append(
            (task_id, vals)
        )

    def write_checkpoint(self, value):
        self.checkpoints.append(value)


class SyncTests(unittest.TestCase):
    def test_preview_never_writes(self):
        odoo = FakeOdoo()

        s = Synchronizer(
            cfg(),
            FILT,
            FakeSource(),
            odoo,
            apply=False,
        )

        stats = s.run()

        self.assertEqual(stats['matched'], 2)
        self.assertEqual(odoo.created, [])
        self.assertEqual(odoo.checkpoints, [])

    def test_apply_idempotent_and_checkpoint(self):
        c, db = cfg(), FakeOdoo()

        s = Synchronizer(
            c,
            FILT,
            FakeSource(),
            db,
            apply=True,
        )

        s.run()

        self.assertEqual(len(db.created), 1)
        self.assertEqual(len(db.checkpoints), 1)
        self.assertEqual(
            parse_checkpoint(db.checkpoints[0]),
            TODAY - timedelta(days=1),
        )

        s2 = Synchronizer(
            c,
            FILT,
            FakeSource(),
            db,
            apply=True,
        )

        s2.run()

        self.assertEqual(len(db.created), 1)
        self.assertEqual(len(db.updated), 0)

    def test_manual_fields_never_updated(self):
        c, db = cfg(), FakeOdoo()

        s = Synchronizer(
            c,
            FILT,
            FakeSource(),
            db,
            apply=True,
        )

        s.run()

        self.assertEqual(
            set(db.created[0]),
            {
                'project_id',
                'name',
                'x_pncp_id',
                'x_objeto',
            },
        )

    def test_apply_blocks_unlinked_existing_tasks(self):
        db = FakeOdoo()
        db.has_unlinked_tasks = lambda: True

        with patch.dict(
            os.environ,
            {'ODOO_ALLOW_UNLINKED_TASKS': ''},
        ):
            with self.assertRaises(ValueError):
                Synchronizer(
                    cfg(),
                    FILT,
                    FakeSource(),
                    db,
                    apply=True,
                ).run()

        self.assertEqual(db.created, [])

    def test_checkpoint_not_advanced_on_failure(self):
        class BrokenSource:
            def list_day(self, *args, **kwargs):
                raise RuntimeError(
                    'Remote service unavailable'
                )

        db = FakeOdoo()

        with self.assertRaises(RuntimeError):
            Synchronizer(
                cfg(),
                FILT,
                BrokenSource(),
                db,
                apply=True,
            ).run()

        self.assertEqual(db.checkpoints, [])


if __name__ == '__main__':
    unittest.main()
