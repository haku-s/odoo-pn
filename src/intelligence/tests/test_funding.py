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
