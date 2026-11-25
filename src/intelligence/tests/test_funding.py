"""Funding snapshots and official facts are independent of news collection."""

import json
import unittest
from datetime import date
from unittest.mock import patch

from intelligence.funding import Evidence, Opportunity, compare, dated_status
from intelligence import funding_sources as sources


class FundingTest(unittest.TestCase):
    def test_elapsed_deadline_does_not_confirm_closure(self):
        self.assertEqual(dated_status(date(2026, 5, 1), None, date(2026, 4, 30)), 'deadline_elapsed')
        self.assertEqual(dated_status(date(2026, 5, 1), date(2026, 5, 2), None), 'future')
        self.assertEqual(dated_status(date(2026, 5, 1), None, None), 'unverified')

    def test_same_identity_for_extension_and_document_revision(self):
        def opportunity(end, value, digest, status='open'):
            return Opportunity('finep:call:123', 'finep', 'Example', 'grant', 'https://example.org/123',
                               {'deadline': Evidence(end, 'https://example.org/123', 'Schedule'),
                                'global_amount': Evidence(value, 'https://example.org/123', 'Funding')},
                               status, {'https://example.org/call.pdf': digest})
        old = opportunity('2026-05-01', 'BRL 1,000', 'a')
        new = opportunity('2026-05-12', 'BRL 2,000', 'b')
        changes = compare([old], [new])
        self.assertEqual(changes[0]['kind'], 'changed')
        self.assertEqual(set(changes[0]['fields']), {'deadline', 'global_amount', 'documents'})
        self.assertEqual(compare([new], [new]), [])
        self.assertEqual(compare([new], [])[0]['kind'], 'not_reverified')
        self.assertEqual(compare([], [new])[0]['kind'], 'new')
        suspended = opportunity('2026-05-12', 'BRL 2,000', 'b', 'suspended')
        self.assertEqual(compare([new], [suspended])[0]['fields'], ['status'])

    def test_schedule_preserves_stages_and_scoped_amounts(self):
        url = 'https://fapesp.br/123'
        item = Opportunity('fapesp:call:123', 'FAPESP', 'Example', 'research_grant', url)
        sources._extract(item, [
            'Prazo para envio da pré-proposta: 15/06/2026',
            'Valor máximo de financiamento previsto por projeto para esta chamada é de R$ 500.000,00.',
            '3) Valor oferecido nesta Chamada', 'Até R$ 50.000.000,00.',
            '4) Requisitos', 'Encerrada',
        ], url, 'Regulation')
        self.assertEqual(item.facts['stage_deadline'].value, '2026-06-15')
        self.assertNotIn('deadline', item.facts)
        self.assertNotIn('official_status', item.facts)
        self.assertEqual(item.facts['global_amount'].value, 'R$ 50.000.000,00')
        self.assertEqual(item.facts['max_per_proposal'].value, 'R$ 500.000,00')

    def test_continuous_requires_explicit_official_evidence(self):
        item = Opportunity('fapesp:pipe:1', 'FAPESP', 'PIPE', 'research_grant', 'https://fapesp.br/pipe/chamadas')
        sources._extract(item, ['Prazo: consulte o programa'], item.url, 'Schedule')
        sources._status(item, date(2026, 11, 25))
        self.assertEqual(item.status, 'unverified')
        sources._extract(item, ['Submissão de propostas: fluxo contínuo'], item.url, 'Schedule')
        sources._status(item, date(2026, 11, 25))
        self.assertEqual(item.status, 'continuous')

    def test_partial_read_does_not_report_missing_facts_or_documents_as_removed(self):
        item = Opportunity('id', 'FAPERJ', 'Example', 'grant', 'https://www.faperj.br/')
        older = Opportunity('id', 'FAPERJ', 'Example', 'grant', item.url, documents={'doc': 'known'})
        older.record('target', 'Companies', item.url, 'Rules')
        self.assertEqual(compare([older], [item]), [])

    def test_source_failure_stays_isolated_and_duplicate_documents_are_unified(self):
        item = Opportunity('id', 'Finep', 'Example', 'grant', 'https://www.finep.gov.br/')
        other = Opportunity('id', 'Finep', 'Reference', 'grant', item.url, documents={'doc': 'hash'})
        def failed(*args):
            raise RuntimeError('Unavailable')
        adapters = {
            'finep': (item.url, lambda *args: ([item], {})),
            'mirror': (item.url, lambda *args: ([other], {})),
            'failed': (item.url, failed),
        }
        with patch.object(sources, 'FUNDING_ADAPTERS', adapters):
            opportunities, audit = sources.collect_opportunities(date(2026, 11, 25), set())
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0].documents, {'doc': 'hash'})
        self.assertEqual(audit['failed']['state'], 'unavailable')

    def test_non_pdf_does_not_record_hash(self):
        item = Opportunity('id', 'Finep', 'Example', 'grant', 'https://www.finep.gov.br/')
        with patch.object(sources, '_get', return_value=b'<html>Denied</html>'):
            sources._pdf(item, 'https://www.finep.gov.br/documents/example.pdf')
        self.assertEqual(item.documents, {})
        self.assertIn('pdf_unavailable_or_unreadable', item.issues)

    def test_finep_initial_catalogue_uses_official_status_independently_of_publication_age(self):
        payload = {'page': 1, 'lastPage': 1, 'totalCount': 2, 'items': [
            {'id': '15', 'titulo': 'Open call', 'situacao': {'name': 'Aberta'},
             'dataDePublicacao': '2025-06-01T00:00:00Z'},
            {'id': '16', 'titulo': 'Closed call', 'situacao': {'name': 'Encerrada'}},
        ]}
        with (patch.object(sources, '_get', return_value=json.dumps(payload).encode()),
              patch.object(sources, '_details')):
            first, _ = sources._finep(date(2026, 11, 25), set())
            tracked, _ = sources._finep(date(2026, 11, 25), {'finep:call:16'})
        self.assertEqual([(item.identity, item.status) for item in first], [('finep:call:15', 'officially_listed_open')])
        self.assertEqual(first[0].facts['publication_date'].value, '2025-06-01')
        self.assertEqual(tracked[1].status, 'closed')


class FundingReliabilityTest(unittest.TestCase):
    def test_pipe_preproposal_is_only_public_entry_deadline(self):
        url = 'https://fapesp.br/pipe/jornada-tecnologica-2026-2a-rodada-fase-1'
        item = Opportunity('fapesp:call:18515', 'FAPESP', 'PIPE', 'research_grant', url)
        sources._extract(item, [
            'Data limite para submissão da pré-proposta: 26/01/2027',
            'Prazo para apresentação da proposta completa para propostas enquadradas: 29/03/2027',
            'Prazo para recurso: 12/04/2027',
            'Data de divulgação dos resultados Junho de 2027',
        ], url, 'Cronograma')
        sources._status(item, date(2027, 2, 1))
        self.assertEqual(item.status, 'deadline_elapsed')
        self.assertEqual(item.facts['full_proposal_deadline'].value, '2027-03-29')
        self.assertEqual(item.facts['appeal_deadline'].value, '2027-04-12')
        self.assertIn('results_schedule', item.facts)

    def test_equally_authoritative_values_remain_visible_regardless_of_order(self):
        for values in (('100', '200'), ('200', '100')):
            item = Opportunity('id', 'Agency', 'Example', 'grant', 'https://example.org/1')
            for value in values:
                item.record('global_amount', value, item.url, 'Regulation')
            self.assertNotIn('global_amount', item.facts)
            self.assertEqual({e.value for e in item.conflicts['global_amount']}, set(values))

    def test_four_cycles_retain_state_during_outage_and_partial_recovery(self):
        from intelligence.funding import CATALOGUE, REGULATION, reconcile

        url = 'https://fapesp.br/123'
        first = Opportunity('fapesp:call:123', 'FAPESP', 'Example', 'research_grant', url,
                            documents={url + '.pdf': 'hash1'})
        first.record('global_amount', 'R$ 100', url, 'Regulation', authority=REGULATION)
        first.record('target', 'Companies with local headquarters', url, 'Regulation', authority=REGULATION)
        initial, _ = reconcile([], [first], '2026-11-25T00:00:00+00:00')
        outage, errors = reconcile(initial, [], '2026-11-25T06:00:00+00:00')
        self.assertEqual(errors, [{'identity': first.identity, 'kind': 'not_reverified'}])
        self.assertEqual(outage[0].checked_at, initial[0].checked_at)
        self.assertEqual(outage[0].facts, first.facts)
        self.assertEqual(outage[0].documents, first.documents)
        self.assertEqual(outage[0].last_attempt_at, '2026-11-25T06:00:00+00:00')

        partial = Opportunity(first.identity, first.institution, first.title, first.modality, url,
                              documents={url + '.pdf': ''})
        partial.record('global_amount', 'R$ 200', url, 'Catalogue', authority=CATALOGUE)
        degraded, changes = reconcile(outage, [partial], '2026-11-25T12:00:00+00:00')
        self.assertEqual(degraded[0].facts, first.facts)
        self.assertEqual(degraded[0].documents, first.documents)
        self.assertEqual(degraded[0].verification, 'partial')
        self.assertEqual({e.value for e in degraded[0].conflicts['global_amount']}, {'R$ 100', 'R$ 200'})
        self.assertEqual(changes[0]['kind'], 'evidence_review')

        recovered = Opportunity(first.identity, first.institution, first.title, first.modality, url,
                                documents={url + '.pdf': 'hash2'})
        recovered.record('global_amount', 'R$ 250', url, 'Regulation', authority=REGULATION)
        recovered.record('target', 'Companies with partners', url, 'Regulation', authority=REGULATION)
        restored, events = reconcile(degraded, [recovered], '2026-11-25T18:00:00+00:00')
        self.assertEqual(restored[0].checked_at, '2026-11-25T18:00:00+00:00')
        self.assertEqual(restored[0].verification, 'verified')
        self.assertEqual(restored[0].facts, recovered.facts)
        self.assertEqual(set(events[0]['fields']), {'global_amount', 'target', 'documents'})
        self.assertEqual(events[1]['kind'], 'evidence_review')
        self.assertEqual(reconcile(restored, [recovered], '2026-11-25T18:00:00+00:00'), (restored, []))

    def test_regulation_and_applicable_revision_win_while_conflicts_remain(self):
        from intelligence.funding import APPLICABLE_REVISION, CATALOGUE, REGULATION

        url = 'https://example.org'
        item = Opportunity('id', 'Agency', 'Example', 'grant', url)
        item.record('deadline', '2026-01-01', url, 'Catalogue', authority=CATALOGUE)
        item.record('deadline', '2026-01-03', url + '/original.pdf', 'Rules', 2, authority=REGULATION)
        item.record('deadline', '2026-01-10', url + '/current.pdf', 'Consolidated correction', 3,
                    authority=APPLICABLE_REVISION)
        self.assertEqual(item.facts['deadline'].value, '2026-01-10')
        self.assertEqual(len(item.conflicts['deadline']), 3)

    def test_explicit_timezone_deadline_and_missing_time(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        url = 'https://example.org'
        item = Opportunity('id', 'Agency', 'Example', 'grant', url)
        sources._extract(item, ['Fim das inscrições: 25/11/2026 às 18h (horário de Brasília)'], url, 'Schedule')
        end = datetime.fromisoformat(item.facts['deadline'].published_at)
        self.assertEqual(end.isoformat(), '2026-11-25T18:00:00-03:00')
        timezone = ZoneInfo('America/Sao_Paulo')
        now = datetime(2026, 11, 25, 18, 1, tzinfo=timezone)
        self.assertEqual(dated_status(now.date(), None, end.date(), now=now, end_at=end), 'deadline_elapsed')
        self.assertEqual(dated_status(now.date(), None, end.date(), now=now), 'within_submission_window')

    def test_eu_two_stage_uses_earliest_date_for_new_applicants(self):
        item = sources._horizon_topic({
            'identifier': ['HORIZON-EXAMPLE-2026'], 'title': ['Example research'],
            'language': ['en'], 'frameworkProgramme': ['43108390'], 'type': ['1'],
            'deadlineModel': ['two-stage'],
            'deadlineDate': ['2026-11-30T17:00:00.000+0000', '2027-03-30T17:00:00.000+0000'],
        }, date(2026, 12, 1), sources.HORIZON_API)
        self.assertEqual(item.status, 'deadline_elapsed')
        self.assertEqual(item.facts['full_proposal_deadline'].value, '2027-03-30')

    def test_ppsus_support_is_a_partial_alternative_to_primary_site(self):
        with (
            patch.object(sources, '_programme', side_effect=RuntimeError('Connection failed')),
            patch.object(sources, '_html', return_value=sources.parse_listing(
                '<p>As propostas devem ser submetidas pelo SISC&amp;T, seguindo o cronograma e as orientações '
                'das Chamadas Públicas.</p>'.encode(),
            )),
        ):
            items, scope = sources._ppsus(date(2026, 11, 25), set())
        self.assertEqual(scope['fallback'], sources.PPSUS_SUPPORT)
        self.assertEqual(items[0].status, 'unverified')
        self.assertIn('primary_programme_unavailable', items[0].issues)


class FundingOdooTest(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        from intelligence.funding_odoo import FundingStore

        self.client = Mock()
        self.store = FundingStore(self.client)
        self.item = Opportunity('id', 'Agency', 'Example', 'grant', 'https://example.org',
                                last_attempt_at='2026-11-25T00:00:00+00:00', verification='partial')

    def test_funding_metadata_does_not_require_an_installed_localization(self):
        from intelligence.config import Settings
        from intelligence.odoo import FUNDING, OdooClient

        client = OdooClient(Settings('https://odoo.example', '', 'key', 50, 3, (1, 1)))
        with patch.object(client, 'call', return_value={}) as call:
            client.fields(FUNDING)
            client.fields('x_publicacoes')
        self.assertEqual(call.call_args_list[0].kwargs['context'], {'active_test': False})
        self.assertEqual(call.call_args_list[1].kwargs['context']['lang'], 'pt_BR')

    def test_committed_write_after_connection_failure_is_confirmed(self):
        from dataclasses import asdict

        row = {'id': 10, 'identity': 'id', 'revision': 1, 'state': asdict(self.item)}
        self.client.call.side_effect = [RuntimeError('Connection failed'), row, [row], {'status': 'confirmed'}, row]
        self.store.append(self.item)
        self.assertEqual(self.store.load(), [self.item])
        self.store.append(self.item)
        first, confirm, _, repeat, _ = self.client.call.call_args_list
        self.assertEqual(first.kwargs['expected_revision'], 0)
        self.assertEqual(confirm.args[1], 'funding_head')
        self.assertEqual(repeat.kwargs['expected_revision'], 1)
        self.assertTrue(all(not key.startswith('x_company') for key in first.kwargs['state']))

    def test_unconfirmed_or_concurrent_write_cannot_replace_head(self):
        from dataclasses import asdict, replace

        other = replace(self.item, title='Different official version')
        row = {'id': 10, 'identity': 'id', 'revision': 1, 'state': asdict(other)}
        self.client.call.side_effect = [{'status': 'stale'}, row]
        with self.assertRaisesRegex(RuntimeError, 'could not be confirmed'):
            self.store.append(self.item)
        self.assertEqual(self.store.revisions, {})

    def test_bad_schema_and_permissions_fail_before_any_write(self):
        self.client.fields.return_value = {}
        with self.assertRaisesRegex(ValueError, 'Unexpected funding field'):
            self.store.check(write=True)
        self.client.call.assert_not_called()
        self.client.fields.side_effect = [
            {key: {'type': kind, 'relation': relation} for key, kind, relation in (
                ('x_name', 'char', None), ('x_identity', 'char', None), ('x_current_id', 'many2one', 'x_revisao_fomento'),
                ('x_generation', 'integer', None), ('x_checked_at', 'datetime', None),
                ('x_last_attempt_at', 'datetime', None), ('x_verification', 'char', None),
            )},
            {'x_opportunity_id': {'type': 'many2one', 'relation': 'x_oportunidade_fomento'},
             'x_revision': {'type': 'integer'}, 'x_state': {'type': 'json'}},
        ]
        self.client.call.side_effect = RuntimeError('Funding collector access denied')
        with self.assertRaisesRegex(RuntimeError, 'access denied'):
            self.store.check(write=True)
        self.assertEqual(self.client.call.call_args.args[1], 'funding_contract')
