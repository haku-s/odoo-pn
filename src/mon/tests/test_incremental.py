import base64
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from unittest.mock import Mock, patch

from mon.checkpoint import days_to_process, parse_checkpoint, serialize_checkpoint
from mon.config import Settings
from mon.pn import Document, PNCPClient, purchase_path
from mon.sync import Synchronizer

from .test_monitor import FILT, RECORD, TODAY, FakeOdoo, FakePNCPSession, FakeResponse, FakeSource, cfg
from .test_runtime_safety import json_response, simulated_client


DAY = TODAY - timedelta(days=5)
THREE = [
    {**RECORD, 'numeroControlePNCP': f'12345678000190-1-{sequence:06}/2026'}
    for sequence in (123, 124, 125)
]
IDENTIFIER = THREE[0]['numeroControlePNCP']


def page(records, pages, total):
    return {'data': records, 'totalPaginas': pages, 'totalRegistros': total}


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeOdoo()
        self.config = cfg(initial_date=DAY, max_days=1)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.enterContext(redirect_stdout(self.stdout))
        self.enterContext(redirect_stderr(self.stderr))

    def run_sync(self, source, apply=True):
        return Synchronizer(self.config, FILT, source, self.db, apply=apply).run()

    def source(self, *replies):
        source = PNCPClient(FakePNCPSession(replies))
        source.documents = Mock(return_value=[])
        source.budget_is_public = Mock(return_value=True)
        return source

    def test_proposal_deadline_requires_seven_full_days_in_preview_and_apply(self):
        self.config.now = lambda: datetime(2026, 11, 20, 13, tzinfo=UTC)
        records = [
            {**row, 'dataEncerramentoProposta': deadline}
            for row, deadline in zip(THREE, ('2024-12-01T10:00:00', '2026-11-27T09:59:59',
                                             '2026-11-27T10:00:00-03:00'), strict=True)
        ]
        absent = {**RECORD, 'numeroControlePNCP': '12345678000190-1-000126/2026'}
        absent.pop('dataEncerramentoProposta')
        records.append(absent)
        for apply in (False, True):
            with self.subTest(apply=apply):
                source = self.source(FakeResponse(data=page(records, 1, 4)), FakeResponse(204))
                stats = self.run_sync(source, apply=apply)
                self.assertEqual(stats['skipped_expired'], 1)
                self.assertEqual(stats['skipped_short_deadline'], 1)
                self.assertEqual(stats['skipped_missing_deadline'], 1)
                self.assertEqual(stats['matched'], 1)
                source.documents.assert_called_once_with(THREE[2]['numeroControlePNCP'])
                self.assertEqual(set(self.db.records), {THREE[2]['numeroControlePNCP']} if apply else set())

    def test_expired_update_blocks_stale_publication_for_the_same_record(self):
        expired = {**RECORD, 'dataEncerramentoProposta': '2024-12-01T10:00:00'}
        source = self.source(FakeResponse(data=page([expired], 1, 1)),
                             FakeResponse(data=page([RECORD], 1, 1)))
        stats = self.run_sync(source)
        self.assertEqual(stats['skipped_expired'], 1)
        self.assertFalse(self.db.created)
        source.documents.assert_not_called()

    def test_apply_rechecks_seven_day_window_before_writing(self):
        deadline = datetime(2026, 11, 27, 13, tzinfo=UTC)
        self.config.now = Mock(side_effect=[deadline - timedelta(days=7),
                                            deadline - timedelta(days=7) + timedelta(seconds=1)])
        source = self.source(FakeResponse(data=page([RECORD], 1, 1)), FakeResponse(204))
        stats = self.run_sync(source)
        self.assertEqual(stats['skipped_short_deadline'], 1)
        self.assertFalse(self.db.created or self.db.updated)

    def test_commits_three_records_before_next_page_and_recovers_after_interrupt_or_timeout(self):
        first = FakeResponse(data=page(THREE, 2, 4))
        fourth = {**RECORD, 'numeroControlePNCP': '12345678000190-1-000126/2026'}
        for error in (KeyboardInterrupt, TimeoutError):
            with self.subTest(error=error):
                self.db = FakeOdoo()
                broken = self.source(first)
                broken.session.get = Mock(side_effect=[first, error()])
                with self.assertRaises(error):
                    self.run_sync(broken)
                self.assertEqual(len(self.db.records), 3)
                self.assertEqual(self.db.checkpoints, [])
                self.assertIn('pages_completed=1', self.stderr.getvalue())
                self.run_sync(self.source(
                    first, FakeResponse(data=page([fourth], 2, 4)), FakeResponse(204),
                ))
                self.assertEqual(len(self.db.created), 4)
                self.assertEqual(self.db.checkpoints, [serialize_checkpoint(DAY)])

    def test_second_page_rate_limit_keeps_first_page_and_resumes_without_duplicates(self):
        body = page(THREE, 2, 4)
        with simulated_client([json_response(body)] + [(429, {}, b'')] * 4) as (source, service):
            source.documents = Mock(return_value=[])
            source.budget_is_public = Mock(return_value=True)
            with self.assertRaisesRegex(RuntimeError, 'page 2: HTTP 429'):
                self.run_sync(source)
            self.assertEqual([stamp for _, stamp in service.attempts], [0, 3, 33, 93, 183])
        self.assertEqual(len(self.db.records), 3)
        self.assertFalse(self.db.checkpoints)
        fourth = {**RECORD, 'numeroControlePNCP': '12345678000190-1-000126/2026'}
        self.run_sync(self.source(
            FakeResponse(data=body), FakeResponse(data=page([fourth], 2, 4)), FakeResponse(204),
        ))
        self.assertEqual(len(self.db.created), 4)
        self.assertEqual(self.db.checkpoints, [serialize_checkpoint(DAY)])

    def test_second_invalid_identifier_blocks_entire_page(self):
        invalid = {**RECORD, 'sequencialCompra': 456}
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.run_sync(self.source(FakeResponse(data=page([THREE[1], invalid], 1, 2))))
        self.assertFalse(self.db.created or self.db.checkpoints)

    def test_task_update_must_be_confirmed_even_after_success_response(self):
        self.db.records[IDENTIFIER] = {'id': 8, 'x_amount': 1.0}
        self.db.update_task = Mock()
        with self.assertRaisesRegex(RuntimeError, '[Uu]nconfirmed task update'):
            self.run_sync(self.source(FakeResponse(data=page([THREE[0]], 1, 1)), FakeResponse(204)))
        self.assertFalse(self.db.checkpoints)

    def test_lost_create_response_and_interruption_reuse_committed_task(self):
        create = self.db.create_task

        def lost_response(values):
            create(values)
            raise KeyboardInterrupt

        self.db.create_task = Mock(side_effect=lost_response)
        replies = (FakeResponse(data=page([THREE[0]], 1, 1)), FakeResponse(204))
        with self.assertRaises(KeyboardInterrupt):
            self.run_sync(self.source(*replies))
        self.assertFalse(self.db.checkpoints)
        self.run_sync(self.source(*replies))
        self.db.create_task.assert_called_once()
        self.assertEqual(self.db.checkpoints, [serialize_checkpoint(DAY)])

    def test_interrupted_confirmed_update_is_not_repeated_on_resume(self):
        self.db.records[IDENTIFIER] = {'id': 8, 'x_amount': 1.0, 'description': 'Manual'}
        update = self.db.update_task

        def interrupted(task_id, values):
            update(task_id, values)
            raise KeyboardInterrupt

        self.db.update_task = Mock(side_effect=interrupted)
        replies = (FakeResponse(data=page([THREE[0]], 1, 1)), FakeResponse(204))
        with self.assertRaises(KeyboardInterrupt):
            self.run_sync(self.source(*replies))
        self.assertEqual(self.db.records[IDENTIFIER]['x_amount'], 379000.0)
        self.assertFalse(self.db.checkpoints)
        self.run_sync(self.source(*replies))
        self.db.update_task.assert_called_once()
        self.assertEqual(self.db.records[IDENTIFIER]['description'], 'Manual')
        self.assertEqual(self.db.checkpoints, [serialize_checkpoint(DAY)])

    def test_second_upload_interruption_reuses_task_and_first_attachment(self):
        path = purchase_path(IDENTIFIER) + '/arquivos'
        docs = [Document(n, f'Fixture-{n}.pdf', f'https://pncp.gov.br/pncp-api{path}/{n}',
                         f'{path}/{n}') for n in (1, 2)]
        bodies = {doc.url: f'%PDF-1.5\nobject {doc.sequence}\n%%EOF\n'.encode() for doc in docs}
        stored = []

        def upload(model, values):
            if len(stored) == 1 and fail:
                raise KeyboardInterrupt
            row = {**values, 'id': len(stored) + 1, 'res_field': False,
                   'checksum': sha1(base64.b64decode(values['raw'])).hexdigest()}
            stored.append(row)
            return row['id']

        self.db.require_access = Mock()
        self.db.attachments = Mock(side_effect=lambda task_id: list(stored))
        self.db.create = Mock(side_effect=upload)
        source = FakeSource()
        source.documents = Mock(return_value=docs)
        source.session = Mock()

        def get(url, **kwargs):
            response = Mock(status_code=200, headers={})
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)
            response.iter_content.return_value = [bodies[url]]
            return response

        source.session.get.side_effect = get
        fail = True
        with self.assertRaises(KeyboardInterrupt):
            self.run_sync(source)
        self.assertEqual(len(stored), 1)
        self.assertFalse(self.db.checkpoints)
        fail = False
        stats = self.run_sync(source)
        self.assertEqual(len(self.db.created), 1)
        self.assertEqual(len(stored), 2)
        self.assertEqual([row['res_id'] for row in stored], [1, 1])
        self.assertEqual(source.session.get.call_count, 4)
        self.assertEqual((stats['documents_uploaded'], stats['documents_present']), (1, 1))
        self.assertEqual(self.db.checkpoints, [serialize_checkpoint(DAY)])

    def test_repeated_identifier_applies_update_once_to_same_task(self):
        publication = {**THREE[0], 'valorTotalEstimado': 11}
        update = {**THREE[0], 'valorTotalEstimado': 22}
        for _ in range(2):
            source = self.source()
            source.session.get = Mock(side_effect=lambda url, **kwargs: FakeResponse(
                data=page([update if url.endswith('/atualizacao') else publication], 1, 1),
            ))
            self.run_sync(source)
            self.assertEqual(len(self.db.created), 1)
            self.assertEqual(self.db.records[IDENTIFIER]['x_amount'], 22)
        self.assertFalse(self.db.updated)

    def test_preview_remains_read_only_with_repeated_identifier_or_interruption(self):
        reply = FakeResponse(data=page([THREE[0]], 1, 1))
        stats = self.run_sync(self.source(reply, reply), apply=False)
        self.assertEqual(stats['requires_create'], 1)
        source = self.source(reply)
        source.session.get = Mock(side_effect=[reply, KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.run_sync(source, apply=False)
        self.assertFalse(self.db.created or self.db.updated or self.db.checkpoints)

    def test_missing_bootstrap_date_requires_configuration_without_checkpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertIsNone(settings.initial_date)
        self.config.initial_date = settings.initial_date
        source = self.source()
        for elapsed in range(4):
            self.config.today = TODAY + timedelta(days=elapsed)
            with self.assertRaisesRegex(ValueError, 'PNCP_INITIAL_DATE'):
                self.run_sync(source)
        self.db.checkpoints.append(serialize_checkpoint(DAY))
        self.run_sync(self.source(FakeResponse(204), FakeResponse(204)))
        self.assertEqual(parse_checkpoint(self.db.checkpoints[-1]), DAY + timedelta(days=1))

    def test_explicit_bootstrap_retries_same_start_across_failed_days(self):
        for elapsed in range(4):
            self.config.today = TODAY + timedelta(days=elapsed)
            source = self.source()
            source.session.get = Mock(side_effect=TimeoutError)
            with self.assertRaises(TimeoutError):
                self.run_sync(source)
            self.assertEqual(source.session.get.call_args.kwargs['params']['dataInicial'],
                             DAY.strftime('%Y%m%d'))
        self.assertFalse(self.db.checkpoints)

    def test_overlap_and_day_budget_keep_next_unfinished_day_reachable(self):
        for overlap, limit, offsets in ((1, 1, [1]), (4, 2, [0, 1]), (2, 4, [-1, 0, 1, 2])):
            with self.subTest(overlap=overlap, limit=limit):
                self.assertEqual(list(days_to_process(None, DAY, TODAY, overlap, limit)),
                                 [DAY + timedelta(days=n) for n in offsets])

    def test_today_checkpoint_leaves_no_work_with_one_day_budget(self):
        self.assertEqual(list(days_to_process(None, TODAY, TODAY, 2, 1)), [])
