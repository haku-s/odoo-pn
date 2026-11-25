import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha1
from unittest.mock import MagicMock, Mock, patch

import requests

from mon.checkpoint import days_to_process, parse_checkpoint
from mon.documents import attach, download, name_and_mime, notice_fields, source_marker
from mon.normalizer import clean_money, normalize, odoo_datetime
from mon.odoo import OdooClient, selection_value
from mon.pn import Document, PaginationChangedError, PNCPClient, purchase_path
from mon.sync import Synchronizer

from .test_monitor import (
    FILT, RECORD, TODAY, FakeOdoo, FakeOdooSession, FakePNCPSession, FakeResponse,
    FakeSource, cfg,
)


class OfficialFieldTests(unittest.TestCase):
    def test_notice_fields_require_explicit_unique_values(self):
        text = (
            'CONCORRÊNCIA Nº 003/2026\n'
            'DATA PARA ABERTURA DE PROPOSTAS: 14/12/2026 ÀS 10:00 HORAS;\n'
            'INÍCIO DA SESSÃO PÚBLICA DE LANCES: 14/12/2026 ÀS 10:01 HORAS (horário de Brasília).\n'
            'CRITÉRIO DE JULGAMENTO:\nMAIOR DESCONTO GLOBAL\n'
            'Prazo para esclarecimentos: até três dias úteis antes da sessão.\n'
        )
        self.assertEqual(notice_fields(text), {
            'notice_number': '003/2026',
            'session_datetime': '2026-12-14 13:01:00',
            'award_basis': 'Global',
        })
        self.assertNotIn('notice_number', notice_fields(text + 'EDITAL Nº 004/2026\n'))
        self.assertEqual(
            notice_fields('Prazo para esclarecimentos: 10/12/2026 às 18:00 (horário de Brasília).'),
            {'clarification_deadline': '2026-12-10 21:00:00'},
        )

    def test_new_official_fields_derive_from_identity_modality_and_bidding_url(self):
        record = normalize({
            **RECORD, 'modalidadeId': 8, 'modoDisputaNome': 'Dispensa Com Disputa',
            'linkSistemaOrigem': 'https://www.novobbmnet.com.br/fixture',
        })
        self.assertEqual(record['contract_type'], 'Dispensa com disputa')
        self.assertEqual(record['dispute_platform'], 'BBMNET')
        self.assertEqual(record['pncp_link'], 'https://pncp.gov.br/app/editais/12345678000190/2026/123')
        self.assertNotIn('notice_number', record)
        self.assertIsNone(normalize({
            **RECORD, 'modalidadeId': 8, 'usuarioNome': 'BBMNET',
            'linkSistemaOrigem': 'https://bbmnet.com.br.example.invalid/',
        }).get('dispute_platform'))

    def test_distinct_identifiers_and_unresolved_criteria(self):
        record = normalize({
            **RECORD, 'processo': '0007/2026', 'numeroProcesso': 'wrong',
            'itensCompra': [{'criterioJulgamentoId': 1}, {'criterioJulgamentoId': 2}],
        })
        self.assertEqual(record['process_number'], '0007/2026')
        self.assertEqual(record['purchase_number'], RECORD['numeroCompra'])
        self.assertNotIn('judgment_criterion', record)

    def test_confidential_zero_and_explicit_public_zero(self):
        for status in (None, 1, 2, 3):
            with self.subTest(status=status):
                raw = {**RECORD, 'valorTotalEstimado': 0}
                if status is not None:
                    raw['orcamentoSigilosoCodigo'] = status
                value = normalize(raw)['estimated_value']
                self.assertEqual(value, None if status in (2, 3) else 0.0)

    def test_invalid_estimate_formats_are_rejected(self):
        for value in (True, '1_000', '1,00', -1, float('inf'), 'N/A'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_money(value)

    def test_datetime_requires_time_and_valid_offset(self):
        self.assertEqual(odoo_datetime('2026-11-27T12:00:00+02:00'),
                         '2026-11-27 10:00:00')
        for value in ('2026-11-27', '2026-11-27T12:00:00+03:99'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                odoo_datetime(value)


class RemoteContractTests(unittest.TestCase):
    def test_criterion_requires_a_complete_unanimous_item_listing(self):
        first = {'numeroItem': 1, 'criterioJulgamentoId': 1, 'criterioJulgamentoNome': 'Menor preço'}
        for second_name, second_id, expected in (
            ('Menor preço', 1, 'Menor preço'), ('Maior desconto', 2, None),
        ):
            with self.subTest(second_name=second_name):
                session = FakePNCPSession([
                    FakeResponse(data=[first]),
                    FakeResponse(data=[{'numeroItem': 2, 'criterioJulgamentoId': second_id,
                                       'criterioJulgamentoNome': second_name}]),
                    FakeResponse(data=[]),
                ])
                with patch('mon.pn.LOOKUP_PAGE_SIZE', 1):
                    self.assertEqual(PNCPClient(session).judgment_criterion(RECORD['numeroControlePNCP']),
                                     expected)

    def test_lookup_includes_archived_tasks_and_verifies_project(self):
        identifier = RECORD['numeroControlePNCP']
        session = FakeOdooSession([FakeResponse(data=[
            {'id': 15, 'name': 'Sample', 'x_pncp_id': identifier,
             'project_id': [11, 'Test'], 'stage_id': [61, 'Initial']},
        ])])
        client = OdooClient(cfg(), session)
        self.assertEqual(client.tasks([identifier])[identifier]['id'], 15)
        self.assertIs(session.calls[0][1]['context']['active_test'], False)

    def test_document_listing_requires_a_list(self):
        for value in (None, {}, [{'sequencialDocumento': 1}]):
            with self.subTest(value=value):
                session = Mock()
                session.get.return_value = FakeResponse(data=value)
                with self.assertRaises((ValueError, RuntimeError)):
                    PNCPClient(session).documents(RECORD['numeroControlePNCP'])


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.task = {
            name: {'string': label, 'type': kind, 'readonly': readonly}
            for name, label, kind, readonly in (
                ('project_id', 'Projeto', 'many2one', False),
                ('user_ids', 'Responsáveis', 'many2many', False),
                ('x_process', 'Número do processo', 'char', False),
                ('x_purchase', 'Numero da licitação', 'char', False),
                ('x_notice', 'Edital / aviso', 'char', False),
                ('x_modality', 'Modalidade', 'selection', False),
                ('x_contract_type', 'Tipo de contratação', 'selection', False),
                ('x_studio_criterio_de_julgamento_1', 'Critério de julgamento', 'selection', False),
                ('x_award', 'Forma de adjudicação', 'char', False),
                ('x_amount', 'Valor estimado', 'float', False),
                ('tag_ids', 'Tags', 'many2many', False),
                ('date_deadline', 'Prazo final', 'datetime', False),
                ('x_session', 'Data e hora da sessão', 'datetime', False),
                ('x_questions', 'Prazo para esclarecimentos', 'datetime', False),
                ('x_company', 'Órgão contratante', 'text', False),
                ('x_location', 'Município / UF', 'char', False),
                ('x_pncp_id', 'Identificador PNCP', 'char', False),
                ('x_platform', 'Plataforma de disputa', 'char', False),
                ('x_link', 'Link oficial do processo', 'char', False),
                ('x_outcome', 'Resultado da Homologação', 'selection', True),
                ('name', 'Title', 'char', False),
                ('stage_id', 'Stage', 'many2one', False),
            )
        }
        self.task['x_modality']['selection'] = [['electronic', 'Electronic Procedure']]
        self.task['x_contract_type']['selection'] = [['bid', 'Licitação']]
        self.task['x_studio_criterio_de_julgamento_1']['selection'] = [['lowest', 'Menor preço']]
        self.task['x_studio_criterio_de_julgamento'] = {
            'string': 'Critério de julgamento', 'type': 'char', 'readonly': False,
        }
        self.task['x_award'] = {
            'type': 'selection', 'readonly': False, 'string': 'Forma de adjudicação',
            'selection': [['lot', 'Lote/Grupo'], ['global', 'Global']],
        }
        self.task['project_id']['relation'] = 'project.project'
        self.task['stage_id']['relation'] = 'project.task.type'
        self.project = {
            'x_pncp_checkpoint': {'type': 'text', 'readonly': False},
            'name': {'type': 'char'}, 'active': {'type': 'boolean'},
        }
        self.stage = {
            'name': {'type': 'char'}, 'active': {'type': 'boolean'},
            'project_ids': {'type': 'many2many', 'relation': 'project.project'},
        }
        self.attachment = {
            name: {'type': kind, 'readonly': name in {'checksum', 'mimetype'}}
            for name, kind in (
                ('name', 'char'), ('description', 'text'), ('checksum', 'char'),
                ('res_model', 'char'), ('res_id', 'many2one_reference'), ('res_field', 'char'),
                ('type', 'selection'), ('raw', 'binary'), ('mimetype', 'char'),
            )
        }
        self.attachment['type']['selection'] = [['binary', 'File'], ['url', 'URL']]
        self.messages = {
            'model': {'type': 'char'}, 'res_id': {'type': 'many2one_reference'},
            'attachment_ids': {'type': 'many2many', 'relation': 'ir.attachment'},
        }
        self.responses = [
            self.task, self.project, self.stage,
            [{'id': 11, 'name': 'Licitações', 'active': True, 'x_pncp_checkpoint': False}],
            [{'id': 61, 'name': 'Capturados', 'active': True, 'project_ids': [11]}],
            self.attachment, self.messages, *[True] * 7,
        ]

    def test_complete_inventory_and_read_only_preflight(self):

        session = FakeOdooSession([FakeResponse(data=value) for value in self.responses])
        client = OdooClient(cfg(field_map={'numero_edital': 'x_purchase'}), session)
        report = []
        client.check_fields(write=True, report=report)
        self.assertEqual(len(report), 20)
        self.assertEqual(
            {row['technical_name'] for row in report if row['write_enabled']},
            {'project_id', 'x_pncp_id', 'x_process', 'x_purchase', 'x_modality',
             'x_amount', 'date_deadline', 'x_company', 'x_location', 'x_notice',
             'x_contract_type', 'x_studio_criterio_de_julgamento_1', 'x_platform', 'x_link',
             'x_session', 'x_questions', 'x_award'},
        )
        self.assertTrue(all(
            request[0].rsplit('/', 1)[-1] in {'fields_get', 'search_read', 'has_access'}
            for request in session.calls
        ))
        self.assertIn('selection', session.calls[0][1]['attributes'])
        self.assertEqual(selection_value(self.task['x_modality'], 'Electronic Procedure'),
                         'electronic')

    def test_bad_types_selection_and_label_collisions_block_writes(self):
        defects = (
            ('date_deadline', {'type': 'date'}),
            ('x_modality', {'selection': [['same', 'One'], ['same', 'Two']]}),
            ('x_other', {'string': 'Numero do Processo', 'type': 'char'}),
            ('x_amount', {'readonly': True}),
        )
        for field, metadata in defects:
            with self.subTest(field=field):
                modified = dict(self.task)
                modified[field] = {**modified.get(field, {}), **metadata}
                client = OdooClient(cfg(), FakeOdooSession([
                    FakeResponse(data=modified), FakeResponse(data=self.project),
                ]))
                with self.assertRaises(ValueError):
                    client.check_fields(write=True)

    def test_legacy_mapping_cannot_target_notice_or_manual_field(self):
        for target in ('x_notice', 'x_platform', 'x_studio_criterio_de_julgamento_1'):
            with self.subTest(target=target):
                client = OdooClient(
                    cfg(field_map={'numero_edital': target}),
                    FakeOdooSession([FakeResponse(data=self.task), FakeResponse(data=self.project)]),
                )
                with self.assertRaisesRegex(ValueError, 'does not match label'):
                    client.check_fields(write=True)

    def test_judgment_criterion_uses_only_the_pinned_selector(self):
        field = 'x_studio_criterio_de_julgamento_1'
        self.task['x_another_criterion'] = self.task[field].copy()
        for present in (True, False):
            with self.subTest(present=present):
                if not present:
                    del self.task[field]
                session = FakeOdooSession([FakeResponse(data=value) for value in self.responses])
                client = OdooClient(cfg(), session)
                client.check_fields(write=True)
                mapped = client.mappings.get('judgment_criterion')
                self.assertEqual(mapped[0] if mapped else None, field if present else None)

    def test_selection_label_mismatch(self):

        with self.assertRaisesRegex(ValueError, 'no unique'):
            selection_value(self.task['x_modality'], 'Unknown official value')

    def test_old_binary_api_is_rejected_before_writes(self):
        session = FakeOdooSession([
            FakeResponse(data=self.task), FakeResponse(data=self.project),
        ], version=19)
        client = OdooClient(cfg(), session)
        report = []
        with self.assertRaisesRegex(ValueError, 'requires Odoo 20'):
            client.check_fields(write=True, report=report)
        self.assertEqual(len(report), 20)
        self.assertFalse(any(entry['write_enabled'] for entry in report))

    def test_stage_association_and_attachment_permissions_are_required(self):
        for index, value in (
            (4, [{'id': 61, 'name': 'Capturados', 'active': True, 'project_ids': [32]}]),
            (13, False),
        ):
            with self.subTest(index=index):
                responses = self.responses.copy()
                responses[index] = value
                report = []
                client = OdooClient(cfg(), FakeOdooSession([FakeResponse(data=x) for x in responses]))
                with self.assertRaises(ValueError):
                    client.check_fields(write=True, report=report)
                self.assertFalse(any(entry['write_enabled'] for entry in report))


class AttachmentTests(unittest.TestCase):
    def setUp(self):

        self.identifier = RECORD['numeroControlePNCP']
        path = purchase_path(self.identifier) + '/arquivos/1'
        self.doc = Document(1, '../Test.pdf', 'https://pncp.gov.br/pncp-api' + path, path)
        self.content = b'%PDF-1.5\nobject\n%%EOF\n'

    def test_foreign_redirect_content_limit_and_incomplete_content(self):

        for status, headers, body, error in (
            (302, {'Location': 'https://foreign.example/first'}, b'', ValueError),
            (200, {'Content-Length': '101'}, b'', ValueError),
            (200, {}, b'x' * 101, ValueError),
            (200, {'Content-Length': '8'}, b'partial', RuntimeError),
            (200, {}, b'', RuntimeError),
            (200, {'Content-Type': 'text/html'}, b'<html>', ValueError),
            (206, {}, self.content, RuntimeError),
        ):
            with self.subTest(status=status, headers=headers):
                session = Mock()
                session.get.return_value = self.response(body, status=status, headers=headers)
                with patch('mon.documents.MAX_BYTES', 100), self.assertRaises(error):
                    download(session, self.doc)
                session.get.assert_called_once()

        session = Mock()
        response = self.response(self.content)
        response.iter_content.side_effect = requests.exceptions.ChunkedEncodingError()
        session.get.return_value = response
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            download(session, self.doc)

    def test_malformed_empty_and_wrong_extension_rejected(self):

        for invalid in (
            b'', b' ', b'%PDF-1.5', b'<html>error</html>', b'plain text',
            self.content + b'\x00unexpected\x00',
        ):
            with self.subTest(content=invalid), self.assertRaises(ValueError):
                name_and_mime(self.doc, invalid)

    def test_pdf_accepts_null_padding_after_end_marker(self):
        _, mime = name_and_mime(self.doc, self.content + b'\x00\r\n')
        self.assertEqual(mime, 'application/pdf')

    @staticmethod
    def response(body, status=200, headers=None):

        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.headers = headers or {}
        response.iter_content.return_value = iter([body])
        return response

    def test_checksum_duplicate_and_same_name_conflict(self):

        db, session = Mock(tasks=Mock(return_value={self.identifier: {'id': 3}})), Mock()
        session.get.side_effect = lambda *a, **k: self.response(self.content)
        digest = sha1(self.content).hexdigest()
        existing = [{'name': 'Existing with a different name.pdf', 'checksum': digest}]
        self.assertFalse(attach(db, session, self.identifier, 3, self.doc, existing))
        existing.append({'name': name_and_mime(self.doc, self.content)[0], 'checksum': '0' * 40})
        with self.assertRaisesRegex(RuntimeError, 'different checksum'):
            attach(db, session, self.identifier, 3, self.doc, existing)
        db.create.assert_not_called()

    def test_confirmed_upload_keeps_previous_revision(self):


        old_doc = replace(self.doc, sequence=2, path=self.doc.path[:-1] + '2')
        existing = [
            {'id': 1, 'name': name_and_mime(self.doc, self.content)[0], 'checksum': '0' * 40,
             'description': source_marker(old_doc)},
        ]
        db, session = Mock(tasks=Mock(return_value={self.identifier: {'id': 3}})), Mock()
        session.get.return_value = self.response(self.content)

        def create(model, values):
            self.assertEqual(model, 'ir.attachment')
            self.assertIn('(PNCP 1)', values['name'])
            db.attachments.return_value = [existing[0], {
                **values, 'id': 2, 'checksum': sha1(self.content).hexdigest(), 'res_field': False,
            }]
            return 2

        db.create.side_effect = create
        self.assertTrue(attach(db, session, self.identifier, 3, self.doc, existing))
        self.assertEqual([item['id'] for item in existing], [1, 2])

    def test_ambiguous_upload_is_confirmed_or_fails_without_retry(self):


        for linked in (True, False):
            with self.subTest(linked=linked):
                db, session = Mock(tasks=Mock(return_value={self.identifier: {'id': 3}})), Mock()
                session.get.return_value = self.response(self.content)
                db.create.side_effect = RuntimeError('Connection dropped')
                db.attachments.return_value = [{
                    'id': 4, 'name': name_and_mime(self.doc, self.content)[0],
                    'checksum': sha1(self.content).hexdigest(), 'description': source_marker(self.doc),
                    'type': 'binary', 'res_field': False, 'res_model': 'project.task',
                    'res_id': 3 if linked else 99,
                }]
                if linked:
                    existing = []
                    self.assertTrue(attach(db, session, self.identifier, 3, self.doc, existing))
                    self.assertFalse(attach(
                        db, Mock(get=Mock(return_value=self.response(self.content))), self.identifier, 3,
                        self.doc, existing,
                    ))
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Unconfirmed'):
                        attach(db, session, self.identifier, 3, self.doc, [])
                db.create.assert_called_once()
                db.attachments.assert_called_once_with(3)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.identifier = RECORD['numeroControlePNCP']
        self.config = cfg(max_days=1)
        self.source = FakeSource()
        self.odoo = FakeOdoo()

    def run_sync(self, apply=True):
        return Synchronizer(
            self.config, FILT, self.source, self.odoo, apply=apply,
        ).run()

    def test_new_task_uses_official_process_organization_and_location(self):
        self.source.list_day = Mock(side_effect=[[{**RECORD, 'processo': '123/2026'}], []])
        self.run_sync()
        self.assertEqual(self.odoo.created[0]['name'], '123/2026 - Sample Organization - Sample City/SP')
        self.assertEqual(self.odoo.created[0]['x_pncp_id'], self.identifier)

    def test_existing_legacy_title_is_migrated_without_changing_stage_or_manual_fields(self):
        name = f'PNCP {self.identifier} - {RECORD["objetoCompra"][:130]}'
        self.odoo.records[self.identifier] = {
            'id': 8, 'name': name, 'x_amount': 379000.0,
            'stage_id': [93, 'Later'], 'description': 'Manual',
        }
        self.source.list_day = Mock(side_effect=[[{**RECORD, 'processo': '123/2026'}], []])
        self.run_sync()
        self.assertEqual(self.odoo.updated, [(8, {'name': '123/2026 - Sample Organization - Sample City/SP'})])
        self.assertEqual(self.odoo.records[self.identifier]['stage_id'], [93, 'Later'])
        self.assertEqual(self.odoo.records[self.identifier]['description'], 'Manual')
        self.assertFalse(self.odoo.created)

    def test_confirmed_document_fields_and_attachment_resume_on_same_task(self):
        path = purchase_path(self.identifier) + '/arquivos/1'
        doc = Document(1, 'Test.pdf', 'https://pncp.gov.br/pncp-api' + path, path, 'Edital')
        content = b'%PDF-1.5\nobject\n%%EOF\n'
        self.source.documents = Mock(return_value=[doc])
        self.source.session = Mock()
        self.source.session.get.side_effect = lambda *a, **k: AttachmentTests.response(content)
        self.odoo.require_access = Mock()
        self.odoo.mappings.update({
            'notice_number': ('x_notice', {'type': 'char'}),
            'award_basis': ('x_award', {'type': 'selection', 'selection': [['global', 'Global']]}),
            'session_datetime': ('x_session', {'type': 'datetime'}),
        })
        existing = {'id': 7, 'name': 'Manual', 'x_amount': 379000.0, 'x_award': 'user-choice',
                    'stage_id': [90, 'Later']}
        self.odoo.records[self.identifier] = existing
        uploaded = []
        self.odoo.attachments = Mock(side_effect=lambda task_id: list(uploaded))

        def create_attachment(model, values):
            uploaded.append({**values, 'id': 11, 'res_field': False, 'checksum': sha1(content).hexdigest()})
            return 11

        self.odoo.create = Mock(side_effect=create_attachment)
        with patch('mon.sync.read_notice', return_value={
            'notice_number': '007/2026', 'award_basis': 'Global', 'session_datetime': '2026-12-14 13:01:00',
        }):
            stats = self.run_sync()
        self.assertEqual(stats['documents_uploaded'], 1)
        self.source.session.get.assert_called_once()
        self.assertEqual(self.odoo.updated, [(7, {'x_notice': '007/2026', 'x_session': '2026-12-14 13:01:00'})])
        self.assertEqual(existing['x_award'], 'user-choice')
        self.assertEqual(existing['stage_id'], [90, 'Later'])
        self.assertFalse(self.odoo.created)
        self.assertEqual(uploaded[0]['res_id'], 7)

    def test_update_only_official_fields_on_archived_task(self):
        task = {
            'id': 7, 'name': 'Manual title', 'description': 'Manual notes',
            'stage_id': [93, 'Later'], 'active': False, 'user_ids': [14],
            'tag_ids': [17], 'priority': '1', 'x_criterion': 'manual',
            'project_id': [self.config.project_id, 'Project'],
            self.config.pncp_field: self.identifier, 'x_amount': 1.0,
            'date_deadline': '2026-11-24 14:00:00',
        }
        self.odoo.records[self.identifier] = task.copy()
        self.odoo.mappings['deadline'] = ('date_deadline', {'type': 'datetime'})
        self.run_sync()
        changes = {'x_amount': 379000.0, 'date_deadline': '2026-11-27 13:00:00'}
        self.assertEqual(self.odoo.updated, [(7, changes)])
        self.assertEqual(self.odoo.records[self.identifier], {**task, **changes})
        self.assertEqual(self.odoo.created, [])

    def test_ambiguous_create_reads_back_after_failure(self):
        original_create = self.odoo.create_task

        def uncertain(values):
            original_create(values)
            raise RuntimeError('Lost create response')

        self.odoo.create_task = Mock(side_effect=uncertain)
        self.odoo.tasks = Mock(wraps=self.odoo.tasks)
        self.assertEqual(self.run_sync()['created'], 1)
        self.assertGreaterEqual(self.odoo.tasks.call_count, 3)
        self.odoo.create_task.assert_called_once()

    def test_ambiguous_write_requires_matching_readback(self):
        self.odoo.records[self.identifier] = {'id': 3, 'x_amount': 2.0}
        original_update = self.odoo.update_task

        def uncertain(task_id, values):
            original_update(task_id, values)
            raise RuntimeError('Lost write response')

        self.odoo.update_task = Mock(side_effect=uncertain)
        self.assertEqual(self.run_sync()['updated'], 1)
        self.odoo.update_task.assert_called_once()

    def test_confirmed_create_response_still_requires_official_readback(self):
        original_create = self.odoo.create_task

        def rewritten(values):
            task_id = original_create(values)
            self.odoo.records[self.identifier]['x_amount'] = 5
            return task_id

        self.odoo.create_task = rewritten
        with self.assertRaisesRegex(RuntimeError, 'official values were not confirmed'):
            self.run_sync()
        self.assertFalse(self.odoo.checkpoints)

    def test_unconfirmed_create_withholds_day(self):
        self.odoo.create_task = Mock(side_effect=RuntimeError('No create response'))
        with self.assertRaisesRegex(RuntimeError, 'Unconfirmed task creation'):
            self.run_sync()
        self.odoo.create_task.assert_called_once()
        self.assertFalse(self.odoo.checkpoints)

    def test_checkpoint_withheld_and_retry_reuses_task_after_attachment_failure(self):
        path = purchase_path(self.identifier) + '/arquivos/1'
        doc = Document(1, 'Test.pdf', 'https://pncp.gov.br/pncp-api' + path, path)
        content = b'%PDF-1.5\nobject\n%%EOF\n'
        checksum = sha1(content).hexdigest()
        self.source.documents = Mock(return_value=[doc])
        self.source.session = Mock()
        self.source.session.get.side_effect = lambda *a, **k: AttachmentTests.response(content)
        self.odoo.require_access = Mock()
        uploaded = []
        self.odoo.attachments = Mock(side_effect=lambda task_id: list(uploaded))

        def create_attachment(model, values):
            uploaded.append({**values, 'id': 2, 'res_field': False, 'checksum': checksum})
            raise RuntimeError('Lost attachment response')

        self.odoo.create = Mock(side_effect=RuntimeError('Upload dropped before commit'))
        with self.assertRaisesRegex(RuntimeError, 'Unconfirmed attachment creation'):
            self.run_sync()
        self.assertFalse(self.odoo.checkpoints)
        self.assertEqual(len(self.odoo.created), 1)

        self.odoo.create.side_effect = create_attachment
        self.assertEqual(self.run_sync()['documents_uploaded'], 1)
        self.assertEqual(len(self.odoo.checkpoints), 1)
        self.assertEqual(len(self.odoo.created), 1)
        self.assertEqual(self.run_sync()['documents_present'], 1)
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(self.odoo.create.call_count, 2)

    def test_missing_listing_and_mismatched_selection_block_completion(self):
        self.source.documents = Mock(side_effect=RuntimeError('Missing listing'))
        with self.assertRaisesRegex(RuntimeError, 'Missing listing'):
            self.run_sync()
        self.assertFalse(self.odoo.created or self.odoo.checkpoints)
        self.source.documents.side_effect = None
        self.source.documents.return_value = []
        self.odoo.mappings = {
            'modality': ('x_modality', {'type': 'selection', 'selection': [['unknown', 'Other']]}),
        }
        with self.assertRaisesRegex(ValueError, 'no unique'):
            self.run_sync()
        self.assertFalse(self.odoo.created or self.odoo.checkpoints)

    def test_preview_counts_updates_and_documents_without_download_or_write(self):
        path = purchase_path(self.identifier) + '/arquivos/1'
        doc = Document(1, 'Test.pdf', 'https://pncp.gov.br/pncp-api' + path, path)
        self.source.documents = Mock(return_value=[doc])
        self.source.session = Mock()
        self.odoo.records[self.identifier] = {'id': 3, 'x_amount': 2.0}
        self.odoo.attachments = Mock(return_value=[])
        stats = self.run_sync(apply=False)
        self.assertEqual(
            {key: stats[key] for key in ('matched', 'requires_update', 'documents_pending')},
            {'matched': 1, 'requires_update': 1, 'documents_pending': 1},
        )
        self.source.session.get.assert_not_called()
        self.assertFalse(self.odoo.created or self.odoo.updated or self.odoo.checkpoints)

    def test_ambiguous_zero_requires_authoritative_lookup(self):
        self.odoo.records[self.identifier] = {'id': 3, 'x_amount': 2.0}
        for public in (False, True):
            with self.subTest(public=public):
                self.source.budget_is_public = Mock(return_value=public)
                self.source.list_day = Mock(side_effect=[
                    [{**RECORD, 'valorTotalEstimado': 0}], [],
                ])
                self.run_sync()
                self.assertEqual(self.odoo.records[self.identifier]['x_amount'], 0.0 if public else 2.0)
                self.source.budget_is_public.assert_called_once_with(self.identifier)
                self.odoo.checkpoints.clear()

    def test_positive_estimate_can_be_partial_and_is_preserved(self):
        self.odoo.records[self.identifier] = {'id': 3, 'x_amount': 2.0}
        self.source.budget_is_public = Mock(return_value=False)
        self.run_sync()
        self.source.budget_is_public.assert_called_once_with(self.identifier)
        self.assertEqual(self.odoo.records[self.identifier]['x_amount'], 2.0)

    def test_failed_budget_lookup_withholds_checkpoint(self):
        self.source.budget_is_public = Mock(side_effect=RuntimeError('Detail lookup failed'))
        with self.assertRaisesRegex(RuntimeError, 'Detail lookup failed'):
            self.run_sync()
        self.assertFalse(self.odoo.created or self.odoo.checkpoints)

    def test_short_pagination_cannot_complete_day(self):
        first = FakeResponse(data={'data': [RECORD], 'totalPaginas': 2, 'totalRegistros': 3})
        other = {**RECORD, 'numeroControlePNCP': '12345678000190-1-000124/2026'}
        second = FakeResponse(data={'data': [other], 'totalPaginas': 2, 'totalRegistros': 3})
        self.source = PNCPClient(FakePNCPSession([first, second] * 2))
        self.source.documents = Mock(return_value=[])
        self.source.budget_is_public = Mock(return_value=True)
        stats = self.run_sync()
        self.assertEqual((stats['days_deferred'], stats['pending_days']), (1, 1))
        self.assertEqual(len(self.odoo.created), 1)
        self.assertIsNone(parse_checkpoint(self.odoo.checkpoints[-1]))

    def test_small_day_limit_can_advance_beyond_overlap(self):
        previous = TODAY - timedelta(days=3)
        self.assertEqual(
            list(days_to_process(previous, previous, TODAY, 2, 1)),
            [previous + timedelta(days=1)],
        )


class AdditionalRemoteContractTests(unittest.TestCase):
    def test_purchase_detail_confidentiality_requires_authoritative_identity_and_code(self):
        identifier = RECORD['numeroControlePNCP']
        session = Mock()
        session.get.side_effect = [
            FakeResponse(data={'numeroControlePNCP': identifier, 'orcamentoSigilosoCodigo': code})
            for code in (1, 2, 3, None)
        ] + [FakeResponse(data={'numeroControlePNCP': 'wrong', 'orcamentoSigilosoCodigo': 1})]
        source = PNCPClient(session)
        self.assertTrue(source.budget_is_public(identifier))
        self.assertFalse(source.budget_is_public(identifier))
        self.assertFalse(source.budget_is_public(identifier))
        with self.assertRaisesRegex(ValueError, 'confidentiality code'):
            source.budget_is_public(identifier)
        with self.assertRaisesRegex(ValueError, 'identity was not confirmed'):
            source.budget_is_public(identifier)
        self.assertEqual(session.get.call_args.args[0],
                         'https://pncp.gov.br/api/consulta' + purchase_path(identifier))

    def test_cross_purchase_document_is_rejected(self):
        session = Mock()
        identifier = RECORD['numeroControlePNCP']
        path = purchase_path(identifier) + '/arquivos/2'
        session.get.return_value = FakeResponse(data=[{
            'sequencialDocumento': 1, 'titulo': 'Official.pdf',
            'tipoDocumentoNome': 'Official', 'url': 'https://pncp.gov.br/pncp-api' + path,
        }])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            PNCPClient(session).documents(identifier)

    def test_lookup_refuses_identifier_in_another_project(self):
        identifier = RECORD['numeroControlePNCP']
        session = FakeOdooSession([FakeResponse(data=[{
            'id': 7, 'x_pncp_id': identifier, 'project_id': [23, 'Other'], 'stage_id': [1, 'Stage'],
        }])])
        with self.assertRaisesRegex(RuntimeError, 'task association'):
            OdooClient(cfg(), session).tasks([identifier])

    def test_attachments_include_chatter_even_without_direct_task_link(self):
        chatter_attachment = {
            'id': 9, 'name': 'Chatter.pdf', 'description': False, 'checksum': '0' * 40,
            'res_model': 'mail.message', 'res_id': 47, 'res_field': False, 'type': 'binary',
        }
        session = FakeOdooSession([
            FakeResponse(data=[]), FakeResponse(data=[{'id': 47, 'attachment_ids': [9]}]),
            FakeResponse(data=[chatter_attachment]),
        ])
        self.assertEqual(OdooClient(cfg(), session).attachments(2), [chatter_attachment])
        self.assertEqual(session.calls[2][1]['domain'], [['id', 'in', [9]]])


class NumericAndPaginationTests(unittest.TestCase):
    def test_server_precision_avoids_repeat_float_updates(self):
        db = FakeOdoo()
        db.records[RECORD['numeroControlePNCP']] = {'id': 3, 'x_amount': 100.01}
        db.mappings['estimated_value'] = ('x_amount', {'type': 'float', 'digits': [16, 2]})
        source = FakeSource()
        source.list_day = Mock(side_effect=[
            [{**RECORD, 'valorTotalEstimado': 100.005, 'orcamentoSigilosoCodigo': 1}], [],
        ])
        Synchronizer(cfg(max_days=1), FILT, source, db, apply=True).run()
        self.assertFalse(db.updated)

    def test_monetary_value_is_rounded_before_creation_and_confirmed_on_readback(self):
        db = FakeOdoo()
        create_task = db.create_task

        def create(values):
            self.assertEqual(values['x_amount'], 100.01)
            task_id = create_task(values)
            db.records[values['x_pncp_id']]['x_currency'] = [18, 'BRL']
            return task_id

        db.create_task = create
        db.task_currency_default = Mock(return_value=18)
        db.currency_step = Mock(return_value=Decimal('0.01'))
        db.mappings['estimated_value'] = (
            'x_amount', {'type': 'monetary', 'currency_field': 'x_currency'},
        )
        source = FakeSource()
        source.list_day = Mock(side_effect=[
            [{**RECORD, 'valorTotalEstimado': 100.005, 'orcamentoSigilosoCodigo': 1}], [],
        ])
        Synchronizer(cfg(max_days=1), FILT, source, db, apply=True).run()
        db.currency_step.assert_called_once_with(18)
        self.assertFalse(db.updated)

    def test_missing_monetary_default_blocks_creation_and_checkpoint(self):
        db = FakeOdoo()
        db.mappings['estimated_value'] = ('x_amount', {'type': 'monetary', 'currency_field': 'x_currency'})
        db.task_currency_default = Mock(side_effect=ValueError('Missing BRL task currency default'))
        db.create_task = Mock(wraps=db.create_task)
        with self.assertRaisesRegex(ValueError, 'Missing BRL task currency default'):
            Synchronizer(cfg(max_days=1), FILT, FakeSource(), db, apply=True).run()
        db.create_task.assert_not_called()
        self.assertFalse(db.checkpoints)

    def test_foreign_currency_cannot_be_used_for_estimate(self):
        session = FakeOdooSession([FakeResponse(data=[
            {'id': 18, 'name': 'USD', 'rounding': 0.01},
        ])])
        with self.assertRaisesRegex(ValueError, 'currency does not match'):
            OdooClient(cfg(), session).currency_step([18, 'USD'])

    def test_currency_repair_requires_a_dedicated_stored_writable_field(self):
        definition = {
            'id': 19, 'name': 'x_currency', 'state': 'manual', 'ttype': 'many2one',
            'relation': 'res.currency', 'readonly': False, 'store': True,
            'related': False, 'compute': False, 'company_dependent': False,
        }
        for related in (False, 'project_id.company_id.currency_id'):
            with self.subTest(related=related):
                session = FakeOdooSession([
                    FakeResponse(data=[{**definition, 'related': related}]),
                    FakeResponse(data=[{'id': 18, 'name': 'BRL', 'rounding': 0.01}]),
                ])
                client = OdooClient(cfg(), session)
                client.currency_amount_siblings = ()
                client.mappings['estimated_value'] = (
                    'x_amount', {'type': 'monetary', 'currency_field': 'x_currency'},
                )
                if related:
                    with self.assertRaisesRegex(ValueError, 'independent'):
                        client.assignable_task_currency()
                    self.assertEqual(len(session.calls), 1)
                else:
                    self.assertEqual(client.assignable_task_currency(), 18)

    def test_task_currency_default_uses_the_same_context_as_creation(self):
        session = FakeOdooSession([FakeResponse(data={'x_currency': 18}), FakeResponse(data=25)])
        client = OdooClient(cfg(), session)
        client.mappings['estimated_value'] = ('x_amount', {'type': 'monetary', 'currency_field': 'x_currency'})
        self.assertEqual(client.task_currency_default(), 18)
        self.assertEqual(client.create_task({'x_amount': 10.0}), 25)
        self.assertEqual(session.calls[0][1]['fields'], ['x_currency'])
        self.assertEqual(session.calls[0][1]['context'], session.calls[1][1]['context'])

    def test_shared_currency_requires_empty_siblings_and_matching_project_currency(self):
        metadata = {
            'id': 19, 'name': 'x_currency', 'state': 'manual', 'ttype': 'many2one',
            'relation': 'res.currency', 'readonly': False, 'store': True,
            'related': False, 'compute': False, 'company_dependent': False,
        }
        for company_currency in ([18, 'BRL'], [20, 'USD']):
            with self.subTest(company_currency=company_currency):
                session = FakeOdooSession([
                    FakeResponse(data={}),
                    FakeResponse(data=[metadata]),
                    FakeResponse(data=[{'id': 18, 'name': 'BRL'}]),
                    FakeResponse(data=[{'id': 11, 'company_id': [4, 'Example']}]),
                    FakeResponse(data=[{'id': 4, 'currency_id': company_currency}]),
                ])
                client = OdooClient(cfg(), session)
                client.currency_amount_siblings = ('x_proposal',)
                client.mappings['estimated_value'] = (
                    'x_amount', {'type': 'monetary', 'currency_field': 'x_currency'},
                )
                self.assertIsNone(client.task_currency_default())
                if company_currency[1] == 'USD':
                    with self.assertRaisesRegex(ValueError, 'project company'):
                        client.assignable_task_currency()
                else:
                    self.assertEqual(client.assignable_task_currency(), 18)
                    self.assertEqual(client.assignable_task_currency({'x_proposal': 0.0}), 18)
                    with self.assertRaisesRegex(ValueError, 'other monetary values'):
                        client.assignable_task_currency({'x_proposal': 50.0})
                self.assertEqual(session.calls[0][1]['fields'], ['x_currency', 'x_proposal'])

    def test_existing_incomplete_task_can_receive_currency_and_amount_together(self):
        identifier = RECORD['numeroControlePNCP']
        db = FakeOdoo()
        db.records[identifier] = {'id': 7, 'name': 'Manual', 'x_currency': False, 'x_amount': 0.0,
                                  'stage_id': [90, 'Later']}
        db.mappings['estimated_value'] = ('x_amount', {'type': 'monetary', 'currency_field': 'x_currency'})
        db.currency_step = Mock(return_value=Decimal('0.01'))
        db.assignable_task_currency = Mock(return_value=18)
        update = db.update_task

        def write(task_id, values):
            update(task_id, values)
            db.records[identifier]['x_currency'] = [18, 'BRL']

        db.update_task = write
        Synchronizer(cfg(max_days=1), FILT, FakeSource(), db, apply=True).run()
        self.assertEqual(db.updated, [(7, {'x_currency': 18, 'x_amount': 379000.0})])
        self.assertEqual(db.records[identifier]['stage_id'], [90, 'Later'])
        self.assertFalse(db.created)

    def test_repeated_consultation_page_is_incomplete(self):
        response = FakeResponse(data={'data': [RECORD], 'totalPaginas': 2, 'totalRegistros': 2})
        source = PNCPClient(FakePNCPSession([response] * 4))
        with self.assertRaisesRegex(PaginationChangedError, 'Repeated record'):
            list(source.list_day('publicacao', TODAY, 6, 50, 3))

    def test_complete_document_pages_and_explicit_empty_end(self):
        identifier = RECORD['numeroControlePNCP']
        path = purchase_path(identifier) + '/arquivos'
        row = {
            'sequencialDocumento': 1, 'titulo': 'Official.pdf', 'tipoDocumentoNome': 'Official',
            'url': 'https://pncp.gov.br/pncp-api' + path + '/1',
        }
        session = Mock()
        session.get.side_effect = [FakeResponse(data=[row]), FakeResponse(204)]
        with patch('mon.pn.LOOKUP_PAGE_SIZE', 1):
            self.assertEqual(len(PNCPClient(session).documents(identifier)), 1)
        self.assertEqual([item.kwargs['params']['pagina'] for item in session.get.call_args_list], [1, 2])

    def test_failed_document_page_is_not_an_empty_end(self):
        identifier = RECORD['numeroControlePNCP']
        path = purchase_path(identifier) + '/arquivos'
        row = {
            'sequencialDocumento': 1, 'titulo': 'Official.pdf', 'tipoDocumentoNome': 'Official',
            'url': 'https://pncp.gov.br/pncp-api' + path + '/1',
        }
        session = Mock()
        session.get.side_effect = [FakeResponse(data=[row]), FakeResponse(404)]
        with patch('mon.pn.LOOKUP_PAGE_SIZE', 1), self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
            PNCPClient(session).documents(identifier)
