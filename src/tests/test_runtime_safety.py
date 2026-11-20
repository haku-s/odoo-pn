import io
import json
import os
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import timedelta
from email.utils import formatdate
from hashlib import sha1
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ProtocolError
from urllib3.response import HTTPResponse

from mon.__main__ import main
from mon.checkpoint import parse_checkpoint, serialize_checkpoint
from mon.config import Filters, Settings
from mon.documents import download
from mon.odoo import OdooClient
from mon.pn import Document, PNCPClient, purchase_path
from mon.sync import Synchronizer
from test_monitor import (
    FILT, RECORD, TODAY, FakeOdoo, FakeSource, cfg,
)


class SimulatedPNCP:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.now = 0.0
        self.attempts = []

    def sleep(self, delay):
        self.now += delay

    def request(self, conn, method, url, *args, **kwargs):
        self.attempts.append((url, self.now))
        return self.response()

    def response(self):
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        status, headers, body = result
        return HTTPResponse(
            status=status, headers=headers, body=io.BytesIO(body),
            preload_content=False, decode_content=False,
        )


@contextmanager
def simulated_client(responses):
    service = SimulatedPNCP(responses)
    with (
        patch.object(HTTPConnectionPool, '_make_request', side_effect=service.request),
        patch('mon.pn.monotonic', side_effect=lambda: service.now),
        patch('mon.pn.sleep', side_effect=service.sleep),
    ):
        client = PNCPClient()
        client.session.trust_env = False
        yield client, service


def json_response(body):
    return 200, {'Content-Type': 'application/json'}, json.dumps(body).encode()


class RequestPacingTests(unittest.TestCase):
    def test_slow_connection_cannot_consume_spacing_between_http_requests(self):
        first_page = json_response({'data': [RECORD], 'totalPaginas': 2, 'totalRegistros': 2})
        connection = HTTPSConnectionPool.ConnectionCls
        for slow_attempt, responses, expected_times in (
            (0, [(429, {'Retry-After': '0'}, b'')] * 4, [4, 7, 10, 13, 16]),
            (1, [(429, {'Retry-After': '0'}, b'')] * 4, [0, 7, 10, 13, 16]),
            (1, [ProtocolError('Simulated response failure')] + [(503, {}, b'')] * 2
             + [(429, {}, b'')], [0, 7, 10, 13, 17]),
        ):
            with self.subTest(slow_attempt=slow_attempt, first_failure=responses[0]):
                service = SimulatedPNCP([first_page, *responses])

                def validate_connection(pool, conn):
                    if len(service.attempts) == slow_attempt:
                        service.now += 4

                def send_request(conn, method, url, *args, **kwargs):
                    page = int(parse_qs(urlsplit(url).query)['pagina'][0])
                    service.attempts.append((page, service.now))

                with (
                    patch.object(HTTPSConnectionPool, '_validate_conn', validate_connection),
                    patch.object(connection, 'request', send_request),
                    patch.object(connection, 'getresponse', lambda conn: service.response()),
                    patch('mon.pn.monotonic', side_effect=lambda: service.now),
                    patch('mon.pn.sleep', side_effect=service.sleep),
                    patch('urllib3.connectionpool.log.warning'),
                ):
                    client = PNCPClient()
                    with client.session:
                        client.session.trust_env = False
                        records = client.list_day('publicacao', TODAY, 6, 50, 3)
                        self.assertEqual(next(records), RECORD)
                        with self.assertRaisesRegex(RuntimeError, 'consultation page 2: HTTP 429'):
                            next(records)
                self.assertEqual(service.attempts, list(zip([1, 2, 2, 2, 2], expected_times)))

    def test_page_two_can_recover_after_rate_limit(self):
        records = [RECORD, {**RECORD, 'numeroControlePNCP': '12345678000190-1-000124/2026'}]
        first, second = [
            json_response({'data': [record], 'totalPaginas': 2, 'totalRegistros': 2})
            for record in records
        ]
        for failures, expected_times in ((1, [0, 3, 33]), (2, [0, 3, 33, 93])):
            with self.subTest(failures=failures), simulated_client(
                [first, *([(429, {}, b'')] * failures), second],
            ) as (client, service):
                self.assertEqual(list(client.list_day('publicacao', TODAY, 6, 50, 3)), records)
                self.assertEqual([
                    (parse_qs(urlsplit(url).query)['pagina'][0], stamp)
                    for url, stamp in service.attempts
                ], list(zip(['1'] + ['2'] * (failures + 1), expected_times)))

    def test_other_http_failures_keep_exponential_backoff(self):
        with simulated_client([(503, {}, b'')] * 3 + [(204, {}, b'')]) as (client, service):
            self.assertEqual(list(client.list_day('publicacao', TODAY, 6, 50, 3)), [])
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 3, 6, 10])

    def test_only_rate_limit_responses_count_towards_progressive_waits(self):
        with simulated_client([
            (503, {}, b''), (429, {}, b''), (429, {}, b''), (204, {}, b''),
        ]) as (client, service):
            self.assertEqual(list(client.list_day('publicacao', TODAY, 6, 50, 3)), [])
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 3, 33, 93])

    def test_retry_after_is_observed_in_seconds_and_http_date(self):
        wall_time = 1_700_000_000
        for header, delay in (('90', 90), (formatdate(wall_time + 6, usegmt=True), 6)):
            with self.subTest(header=header), patch(
                'urllib3.util.retry.time.time', return_value=wall_time,
            ), simulated_client([
                (429, {'Retry-After': header}, b''), (204, {}, b''),
            ]) as (client, service):
                self.assertEqual(list(client.list_day('publicacao', TODAY, 6, 50, 3)), [])
                self.assertEqual([stamp for _, stamp in service.attempts], [0, delay])

    def test_persistent_rate_limit_stops_after_four_attempts(self):
        with simulated_client([(429, {}, b'')] * 4) as (client, service):
            with self.assertRaisesRegex(RuntimeError, 'consultation page 1: HTTP 429'):
                list(client.list_day('publicacao', TODAY, 6, 50, 3))
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 30, 90, 180])

    def test_retry_after_over_local_limit_stops_instead_of_retrying_early(self):
        for status, header in ((429, '91'), (503, '31')):
            with self.subTest(status=status), simulated_client([
                (status, {'Retry-After': header}, b''),
            ]) as (client, service):
                with self.assertRaisesRegex(RuntimeError, f'consultation page 1: HTTP {status}'):
                    list(client.list_day('publicacao', TODAY, 6, 50, 3))
                self.assertEqual([stamp for _, stamp in service.attempts], [0])

    def test_invalid_retry_after_aborts_without_retrying(self):
        with simulated_client([(429, {'Retry-After': 'invalid'}, b'')]) as (client, service):
            with self.assertRaisesRegex(RuntimeError, 'consultation page 1: request failed'):
                list(client.list_day('publicacao', TODAY, 6, 50, 3))
        self.assertEqual([stamp for _, stamp in service.attempts], [0])

    def test_pages_lookups_and_download_redirects_share_the_interval(self):
        identifier = RECORD['numeroControlePNCP']
        path = purchase_path(identifier) + '/arquivos/1'
        doc_url = 'https://pncp.gov.br/pncp-api' + path
        binary = b'%PDF-1.5\nobject\n%%EOF\n'
        second = {**RECORD, 'numeroControlePNCP': '12345678000190-1-000124/2026'}
        responses = [
            json_response({'data': [row], 'totalPaginas': 2, 'totalRegistros': 2})
            for row in (RECORD, second)
        ] + [
            json_response({'numeroControlePNCP': identifier, 'orcamentoSigilosoCodigo': 1}),
            json_response([{'sequencialDocumento': 1, 'titulo': 'Fixture.pdf',
                            'tipoDocumentoNome': 'Official', 'url': doc_url}]),
            (204, {}, b''),
            (302, {'Location': 'https://pncp.gov.br/api/pncp' + path, 'Retry-After': '6'}, b''),
            (429, {'Retry-After': '2'}, b''),
            (200, {'Content-Length': str(len(binary))}, binary),
        ]
        with patch('mon.pn.LOOKUP_PAGE_SIZE', 1), simulated_client(responses) as (client, service):
            self.assertEqual(len(list(client.list_day('publicacao', TODAY, 6, 50, 3))), 2)
            self.assertTrue(client.budget_is_public(identifier))
            document, = client.documents(identifier)
            self.assertEqual(download(client.session, document), binary)
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 3, 6, 9, 12, 15, 21, 24])

    def test_download_can_recover_on_the_last_rate_limit_retry(self):
        path = purchase_path(RECORD['numeroControlePNCP']) + '/arquivos/1'
        document = Document(1, 'Fixture.pdf', 'https://pncp.gov.br/pncp-api' + path, path)
        binary = b'%PDF-1.5\nobject\n%%EOF\n'
        for headers, times in (({}, [0, 30, 90, 180]), ({'Retry-After': '90'}, [0, 90, 180, 270])):
            with (
                self.subTest(headers=headers),
                simulated_client([(429, headers, b'')] * 3 + [(200, {}, binary)]) as (client, service),
                patch('mon.documents.monotonic', side_effect=lambda: service.now),
            ):
                self.assertEqual(download(client.session, document), binary)
                self.assertEqual([stamp for _, stamp in service.attempts], times)

    def test_requests_are_paced_from_the_end_of_body_reads(self):
        identifier = RECORD['numeroControlePNCP']
        path = purchase_path(identifier) + '/arquivos/1'
        document = Document(1, 'Fixture.pdf', 'https://pncp.gov.br/pncp-api' + path, path)
        consultation = json_response({'data': [RECORD], 'totalPaginas': 1})
        budget = json_response({'numeroControlePNCP': identifier, 'orcamentoSigilosoCodigo': 1})
        original_iter_content = requests.Response.iter_content

        def slow_body(response, *args, **kwargs):
            yield from original_iter_content(response, *args, **kwargs)
            service.now += 4

        with (
            simulated_client([consultation, (200, {}, b'fixture'), budget]) as (client, service),
            patch.object(requests.Response, 'iter_content', slow_body),
        ):
            self.assertEqual(list(client.list_day('publicacao', TODAY, 6, 50, 3)), [RECORD])
            self.assertEqual(download(client.session, document), b'fixture')
            self.assertTrue(client.budget_is_public(identifier))
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 7, 14])

    def test_download_retry_allowance_does_not_extend_the_body_time_limit(self):
        path = purchase_path(RECORD['numeroControlePNCP']) + '/arquivos/1'
        document = Document(1, 'Fixture.pdf', 'https://pncp.gov.br/pncp-api' + path, path)

        def slow_body(response, **kwargs):
            service.now += 121
            yield b'fixture'

        with (
            simulated_client([(429, {}, b''), (200, {}, b'fixture')]) as (client, service),
            patch('mon.documents.monotonic', side_effect=lambda: service.now),
            patch.object(requests.Response, 'iter_content', slow_body),
        ):
            with self.assertRaisesRegex(RuntimeError, 'download exceeded its time limit'):
                download(client.session, document)
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 30])

    def test_incomplete_document_listing_with_rate_limit_is_an_error(self):
        identifier = RECORD['numeroControlePNCP']
        doc_url = 'https://pncp.gov.br/pncp-api' + purchase_path(identifier) + '/arquivos/1'
        responses = [
            json_response([{'sequencialDocumento': 1, 'titulo': 'Fixture.pdf',
                            'tipoDocumentoNome': 'Official', 'url': doc_url}]),
        ] + [(429, {}, b'')] * 4
        with patch('mon.pn.LOOKUP_PAGE_SIZE', 1), simulated_client(responses) as (client, service):
            with self.assertRaisesRegex(RuntimeError, 'document listing page 2: HTTP 429'):
                client.documents(identifier)
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 3, 33, 93, 183])


class ResumeTests(unittest.TestCase):
    def test_rate_limited_download_preserves_checkpoint_and_reuses_task(self):
        completed_day = TODAY - timedelta(days=2)
        failed_day = TODAY - timedelta(days=1)
        config = cfg(initial_date=completed_day, overlap_days=1, max_days=2)
        identifier = RECORD['numeroControlePNCP']
        path = purchase_path(identifier) + '/arquivos/1'
        document = Document(1, 'Fixture.pdf', 'https://pncp.gov.br/pncp-api' + path, path)
        source, db = FakeSource(), FakeOdoo()
        source.list_day = Mock(side_effect=lambda kind, day, *args, **kwargs: (
            [RECORD] if (kind, day) == ('publicacao', failed_day) else []
        ))
        source.documents = Mock(return_value=[document])
        db.checkpoints.append(serialize_checkpoint(completed_day))
        db.require_access = Mock()
        db.attachments = Mock(return_value=[])
        db.create = Mock()

        with (
            simulated_client([(429, {}, b'')] * 4) as (client, service),
            self.assertRaisesRegex(RuntimeError, 'document download: HTTP 429'),
        ):
            source.session = client.session
            Synchronizer(config, FILT, source, db, apply=True).run()
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 30, 90, 180])
        self.assertEqual([parse_checkpoint(item) for item in db.checkpoints], [completed_day])
        self.assertEqual(len(db.created), 1)
        db.create.assert_not_called()

        binary = b'%PDF-1.5\nobject\n%%EOF\n'

        def create_attachment(model, values):
            db.attachments.return_value = [{
                **values, 'id': 2, 'res_field': False, 'checksum': sha1(binary).hexdigest(),
            }]
            return 2

        db.create.side_effect = create_attachment
        with simulated_client([(200, {}, binary)]) as (client, service):
            source.session = client.session
            stats = Synchronizer(config, FILT, source, db, apply=True).run()
        self.assertEqual(stats['documents_uploaded'], 1)
        self.assertEqual([parse_checkpoint(item) for item in db.checkpoints],
                         [completed_day, failed_day])
        self.assertEqual(len(db.created), 1)
        db.create.assert_called_once()

    def test_completed_day_survives_partial_page_and_interruptions_in_later_modality(self):
        completed_day = TODAY - timedelta(days=2)
        failed_day = TODAY - timedelta(days=1)
        filters = Filters((6, 8), FILT.include_any, (), ())
        config = cfg(initial_date=completed_day, overlap_days=2, max_days=3)
        identifier = '12345678000190-1-000124/2026'
        new_record = {**RECORD, 'numeroControlePNCP': identifier}

        replies = [json_response({'data': [new_record], 'totalPaginas': 2})] + [(429, {}, b'')] * 4
        for failure in ('page', 'interrupt'):
            with (
                self.subTest(failure=failure), redirect_stdout(io.StringIO()) as stdout,
                simulated_client(replies) as (page_client, service),
            ):
                db, source = FakeOdoo(), FakeSource()

                def partial(kind, day, modality, page_size, max_pages, uf=None):
                    if (kind, day, modality) == ('publicacao', completed_day, 6):
                        yield RECORD
                    if (kind, day, modality) == ('atualizacao', failed_day, 8):
                        if failure == 'interrupt':
                            raise KeyboardInterrupt
                        yield from page_client.list_day(kind, day, modality, page_size, max_pages, uf)

                source.list_day = partial
                error = RuntimeError if failure == 'page' else KeyboardInterrupt
                with self.assertRaises(error):
                    Synchronizer(config, filters, source, db, apply=True).run()
                if failure == 'page':
                    self.assertEqual([stamp for _, stamp in service.attempts], [0, 3, 33, 93, 183])
                self.assertEqual([parse_checkpoint(item) for item in db.checkpoints], [completed_day])
                self.assertNotIn(failed_day.isoformat(), stdout.getvalue())
                self.assertNotIn(identifier, db.records)

                def recovered(kind, day, modality, *args, **kwargs):
                    if (kind, day, modality) == ('atualizacao', failed_day, 8):
                        return [new_record]
                    if (kind, day, modality) == ('publicacao', completed_day, 6):
                        return [RECORD]
                    return []

                source.list_day = Mock(side_effect=recovered)
                Synchronizer(config, filters, source, db, apply=True).run()
                self.assertIn(failed_day, [call.args[1] for call in source.list_day.call_args_list])
                self.assertEqual([parse_checkpoint(item) for item in db.checkpoints],
                                 [completed_day, failed_day])
                self.assertEqual(len(db.created), 2)
                self.assertIn(identifier, db.records)


class ConfigurationAndOutputTests(unittest.TestCase):
    def test_blank_field_map_is_empty(self):
        for value in (None, '', '  \n\t'):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ['ODOO_FIELD_MAP'] = value
                self.assertEqual(Settings.from_env().field_map, {})

    def test_field_map_validation_is_preserved(self):
        value = '  {"numero_processo":"x_process"}  '
        with patch.dict(os.environ, {'ODOO_FIELD_MAP': value}, clear=True):
            self.assertEqual(Settings.from_env().field_map, {'numero_processo': 'x_process'})
        for value in ('{', 'null', '[]', '{"numero_processo":1}',
                      '{"numero_processo":"name"}', '{"unknown":"x_invalid"}'):
            with self.subTest(value=value), patch.dict(
                os.environ, {'ODOO_FIELD_MAP': value}, clear=True,
            ), self.assertRaises(ValueError):
                Settings.from_env()

    def test_configuration_failure_does_not_echo_internal_values(self):
        internal_value = 'https://private-instance.example.invalid'
        for variable in ('ODOO_PROJECT_ID', 'ODOO_STAGE_ID', 'PNCP_PAGE_SIZE', 'PNCP_INITIAL_DATE'):
            with self.subTest(variable=variable), patch.dict(
                os.environ, {variable: internal_value}, clear=True,
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(['preview']), 1)
                self.assertIn(variable, stderr.getvalue())
                self.assertNotIn(internal_value, stderr.getvalue())

    def test_inspect_emits_counts_without_field_inventory(self):
        def check_fields(write, report):
            report.append({
                'technical_name': 'x_internal_field', 'label': 'Private organization',
                'status': 'validated', 'write_enabled': True,
            })

        with (
            patch('mon.__main__.OdooClient') as client,
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            client.return_value.check_fields.side_effect = check_fields
            self.assertEqual(main(['inspect']), 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output['status'], 'passed')
        self.assertEqual(output['counts']['fields_checked'], 1)
        self.assertNotIn('x_internal_field', stdout.getvalue())
        self.assertNotIn('Private organization', stdout.getvalue())
        self.assertEqual(stderr.getvalue(), '')

    def test_inspect_reports_failed_request_without_remote_details(self):
        config = cfg()
        private_details = f'{config.odoo_url} x_internal_field Private-document.pdf'
        session = Mock()
        session.post.side_effect = requests.ConnectionError(private_details)
        client = OdooClient(config, session)
        with (
            patch('mon.__main__.OdooClient', return_value=client),
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main(['inspect']), 1)
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'failed')
        self.assertIn('project.task/fields_get', stderr.getvalue())
        self.assertIn('connection failed', stderr.getvalue())
        for value in private_details.split():
            self.assertNotIn(value, stdout.getvalue() + stderr.getvalue())

    def test_preview_logs_only_counts_and_performs_no_writes_or_download(self):
        config, db, source = cfg(max_days=1), FakeOdoo(), FakeSource()
        identifier = RECORD['numeroControlePNCP']
        path = purchase_path(identifier) + '/arquivos/1'
        title = 'Private-document.pdf'
        source.documents = Mock(return_value=[
            Document(1, title, 'https://pncp.gov.br/pncp-api' + path, path),
        ])
        source.session = Mock()
        db.create = Mock()
        with (
            patch('mon.__main__.Settings.from_env', return_value=config),
            patch('mon.__main__.Filters.load', return_value=FILT),
            patch('mon.__main__.PNCPClient', return_value=source),
            patch('mon.__main__.OdooClient', return_value=db),
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main(['preview']), 0)
        output = stdout.getvalue()
        counts = json.loads(output.splitlines()[-1])['counts']
        self.assertEqual(counts['matched'], 1)
        self.assertEqual(counts['documents_pending'], 1)
        for value in (identifier, title, config.odoo_url, RECORD['objetoCompra']):
            self.assertNotIn(value, output)
        self.assertEqual(stderr.getvalue(), '')
        self.assertFalse(db.created or db.updated or db.checkpoints)
        db.create.assert_not_called()
        source.session.get.assert_not_called()

    def test_rate_limited_preview_reports_failure_without_completed_day(self):
        config, db = cfg(max_days=1), FakeOdoo()
        db.create = Mock()
        replies = [json_response({'data': [RECORD], 'totalPaginas': 2})] + [(429, {}, b'')] * 4
        with (
            patch('mon.__main__.Settings.from_env', return_value=config),
            patch('mon.__main__.Filters.load', return_value=FILT),
            simulated_client(replies) as (client, service),
            patch('mon.__main__.PNCPClient', return_value=client),
            patch('mon.__main__.OdooClient', return_value=db),
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main(['preview']), 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), 'Error in collection: PNCP consultation page 2: HTTP 429\n')
        self.assertEqual([stamp for _, stamp in service.attempts], [0, 3, 33, 93, 183])
        self.assertFalse(db.created or db.updated or db.checkpoints)
        db.create.assert_not_called()

    def test_workflow_uses_only_secret_for_odoo_url(self):
        workflow = Path(__file__).resolve().parents[2] / '.github/workflows/sync.yml'
        references = [
            line.strip() for line in workflow.read_text().splitlines()
            if 'ODOO_URL' in line
        ]
        self.assertEqual(references, ['ODOO_URL: ${{ secrets.ODOO_URL }}'])
