from collections import Counter
from contextlib import redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from intelligence.__main__ import main
from intelligence.science import (
    Search, crossref, doi, load_snapshot, openalex, pubmed, pubmed_record, radars, reconcile, scielo_article, xml_records,
)


class ScienceTests(unittest.TestCase):
    def test_pubmed_uses_creation_modification_and_mesh_and_keeps_old_publication(self):
        record = b'''<?xml version="1.0"?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>
<Article><ArticleTitle>A <i>psychometric</i> instrument</ArticleTitle>
<ArticleDate DateType="Electronic"><Year>2019</Year><Month>01</Month><Day>02</Day></ArticleDate>
<PublicationTypeList><PublicationType>Observational Study</PublicationType></PublicationTypeList>
<Abstract><AbstractText>Protected abstract</AbstractText></Abstract></Article>
<CommentsCorrectionsList><CommentsCorrections RefType="ErratumIn"><PMID>321</PMID></CommentsCorrections></CommentsCorrectionsList>
</MedlineCitation><PubmedData><History><PubMedPubDate PubStatus="entrez"><Year>2026</Year><Month>11</Month><Day>01</Day></PubMedPubDate></History>
<ArticleIdList><ArticleId IdType="doi">10.9999/EXAMPLE</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>'''
        search = Search('sample', 'psychometrics[MeSH Terms]', ('psychometric',), ())
        with patch('intelligence.science.read', side_effect=[
            b'{"esearchresult":{"count":"2","retstart":"0","idlist":["123"]}}', record,
            b'{"esearchresult":{"count":"2","retstart":"1","idlist":["124"]}}', b'<PubmedArticleSet/>',
        ]) as fetch:
            stats = Counter()
            rows = list(pubmed(search, date(2026, 11, 1), date(2026, 11, 2), stats))
        term = fetch.call_args_list[0].args[1]['term']
        for tag in ('[crdt]', '[mdat]', '[mhda]'):
            self.assertIn(tag, term)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['dates']['published'], '2019-01-02')
        self.assertEqual(rows[0]['dates']['pubmed_created'], '2026-11-01')
        self.assertEqual(rows[0]['notices'], [{'type': 'ErratumIn', 'pmid': '321'}])
        self.assertNotIn('abstract', rows[0])
        self.assertEqual(rows[0]['kind'], 'observational_study')
        self.assertEqual(stats['missing_records'], 1)

    def test_xml_rejects_custom_dtd_and_unknown_root(self):
        with self.assertRaises(ValueError):
            xml_records(b'<!DOCTYPE PubmedArticleSet [<!ENTITY x "abc">]><PubmedArticleSet/>')
        with self.assertRaises(ValueError):
            xml_records(b'<html>Access denied</html>')

    def test_partial_publication_date_retains_precision(self):
        row = pubmed_record(ET.fromstring('''<PubmedArticle><MedlineCitation><PMID>456</PMID>
<Article><ArticleTitle>Assessment</ArticleTitle><Journal><JournalIssue><PubDate><Year>2020</Year>
<Month>Jan</Month></PubDate></JournalIssue></Journal></Article></MedlineCitation></PubmedArticle>'''))
        self.assertEqual(row['dates']['published'], '2020-01')

    def test_crossref_uses_cursor_and_requires_thematic_evidence(self):
        def response(items, cursor):
            return json.dumps({'status': 'ok', 'message': {
                'total-results': 40, 'items': items, 'next-cursor': cursor,
            }}).encode()

        items = [{'DOI': '10.9999/one', 'title': ['Testing an assistive technology'],
                  'type': 'posted-content', 'subtype': 'preprint',
                  'indexed': {'date-time': '2026-11-02T00:00:00Z'}}]
        items += [{'DOI': f'10.9999/{number}', 'title': ['Unrelated technology']}
                  for number in range(19)]
        with patch('intelligence.science.read', side_effect=[response(items, 'next'), response([], 'next')]) as fetch:
            stats = Counter()
            rows = list(crossref('assistive technology', date(2026, 11, 1), date(2026, 11, 2), stats))
        self.assertEqual(fetch.call_args_list[1].args[1]['cursor'], 'next')
        self.assertIn('from-index-date', fetch.call_args_list[0].args[1]['filter'])
        self.assertEqual(stats['outside_topic'], 19)
        self.assertEqual(rows[0]['kind'], 'preprint')

    def test_identity_preserves_related_notices_and_partial_source_failure(self):
        original = {'doi': '10.9999/one', 'pmid': '12', 'kind': 'article'}
        linked = {'doi': '10.9999/one', 'kind': 'article'}
        correction = {'doi': '10.9999/two', 'kind': 'correction'}
        initial = reconcile([], [('pubmed', original), ('crossref', linked), ('crossref', correction)])
        self.assertEqual(len(initial), 2)
        self.assertEqual(initial, reconcile(initial, []))
        self.assertEqual(initial, reconcile(initial, [('crossref', linked)]))
        updated = dict(linked, notices=[{'type': 'retraction'}])
        recovered = reconcile(initial, [('crossref', updated)])
        self.assertEqual(len(recovered), 2)
        self.assertEqual(recovered, reconcile(recovered, [('crossref', updated)]))
        self.assertEqual(doi('https://doi.org/10.9999%2FONE'), '10.9999/one')

    def test_preprint_and_article_sharing_only_title_are_distinct(self):
        a = {'doi': '10.9999/one', 'title': 'Same title', 'kind': 'preprint'}
        b = {'doi': '10.9999/two', 'title': 'Same title', 'kind': 'article'}
        self.assertEqual(len(reconcile([], [('crossref', a), ('crossref', b)])), 2)

    def test_crossref_notice_identity_and_explicit_abstract_license(self):
        original = '10.9999/original'
        citation = {
            'DOI': '10.9999/notice', 'title': ['Correction: Assistive technology'],
            'type': 'journal-article', 'abstract': '<jats:p>A correction.</jats:p>',
            'relation': {'is-version-of': [{'id-type': 'doi', 'id': original}]},
            'update-to': [{'type': 'correction', 'DOI': original}],
            'license': [{'URL': 'https://creativecommons.org/licenses/by/4.0/',
                         'content-version': 'vor', 'start': {'date-parts': [[2020, 1, 1]]}}],
        }
        def collect():
            response = {'status': 'ok', 'message': {'items': [citation]}}
            with patch('intelligence.science.read', return_value=json.dumps(response).encode()):
                return next(crossref('assistive technology', date(2026, 11, 1), date(2026, 11, 2), Counter()))
        row = collect()
        self.assertEqual((row['doi'], row['kind'], row['notices'][0]['doi']), ('10.9999/notice', 'correction', original))
        self.assertEqual(row['abstract'], 'A correction.')
        citation['license'][0]['content-version'] = 'tdm'
        self.assertNotIn('abstract', collect())

    def test_openalex_keeps_non_primary_works_but_does_not_assert_peer_review(self):
        first = {'id': 'https://openalex.org/W1', 'ids': {'doi': 'https://doi.org/10.5281/zenodo.123'},
                 'display_name': 'Avaliação neuropsicológica', 'type': 'article', 'indexed_in': ['datacite'],
                 'open_access': {'is_oa': True}, 'publication_date': '2026-11-01'}
        second = first | {'id': 'https://openalex.org/W2', 'indexed_in': ['crossref']}
        response = {'meta': {'count': 30}, 'results': [first, second]}
        with patch('intelligence.science.read', return_value=json.dumps(response).encode()) as read:
            stats = Counter()
            rows = list(openalex('avaliação neuropsicológica', date(2026, 11, 1), date(2026, 11, 2), stats))
        self.assertIn('from_publication_date', read.call_args.args[1]['filter'])
        self.assertEqual([row['kind'] for row in rows], ['unknown'])
        self.assertEqual(rows[0]['doi'], '10.5281/zenodo.123')
        self.assertEqual(stats['page_caps'], 1)

    def test_radar_archive_uses_edition_identity_even_with_shared_catalog_link(self):
        fixture = b'''<section id="historico"><div class="hist-item">
<span class="hist-year">2020</span><div class="hist-title">Ed. 21 - Inteligencia Artificial</div>
<a href="https://www.gov.br/inpi/pt-br/assuntos/informacao/radares-tecnologicos">Catalog</a>
</div></section>'''
        with patch('intelligence.science.read', return_value=fixture):
            rows = list(radars(Counter()))
        self.assertEqual(rows[0]['official_id'], 'inpi-radar:21')
        self.assertEqual(rows[0]['dates'], {'published': '2020'})

    def test_scielo_crosswalk_requires_the_official_pid_and_rejects_conflicting_doi(self):
        candidate = {'scielo_pid': 'S0101-60832011000300005', 'doi': '10.1590/example',
                     'title': 'Avaliação neuropsicológica', 'topics': ['assessment'], 'url': 'https://doi.org/10.1590/example'}
        row = {'collection': 'scl', 'code': candidate['scielo_pid'], 'document_type': 'review-article',
               'article': {'v12': [{'_': candidate['title']}], 'v40': [{'_': 'pt'}]}}
        with patch('intelligence.science.read', return_value=json.dumps(row).encode()):
            result = scielo_article(candidate, Counter())
        self.assertEqual((result['doi'], result['kind']), ('10.1590/example', 'review'))
        row['doi'] = '10.1590/other'
        with (patch('intelligence.science.read', return_value=json.dumps(row).encode()),
              self.assertRaisesRegex(ValueError, 'DOI conflict')):
            scielo_article(candidate, Counter())

    def test_summary_preview_preserves_private_snapshot_without_odoo_writes(self):
        result = {'mode': 'science-preview', 'status': 'partial', 'records': [
            {'observations': {'pubmed': {'pmid': '1', 'title': 'Sample', 'dates': {}}}},
        ]}
        with (TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as stdout,
              patch('intelligence.science.preview', return_value=result),
              patch('intelligence.__main__.Settings.from_env'),
              patch('intelligence.__main__.OdooClient') as odoo_type):
            odoo = odoo_type.return_value
            odoo.assuntos.return_value = [object()]
            odoo.fields.return_value = {'x_example': {'type': 'char'}}
            odoo.selection.return_value = ('Notícia',)
            path = Path(directory) / 'snapshot.json'
            self.assertEqual(main(['science-preview', '--summary-only', '--science-odoo', '--science-output', str(path)]), 0)
            self.assertEqual(load_snapshot(path)[0]['observations']['pubmed']['pmid'], '1')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('x_example', stdout.getvalue())
            self.assertNotIn('Sample', stdout.getvalue())
            self.assertEqual([call[0] for call in odoo.method_calls], ['assuntos', 'fields', 'selection'])
