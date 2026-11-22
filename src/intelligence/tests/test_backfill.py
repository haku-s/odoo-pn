from contextlib import redirect_stderr
from io import StringIO
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from intelligence import backfill as module

ARTICLE = 'https://agenciabrasil.ebc.com.br/saude/noticia/2024-10/example'
IMAGE = 'https://agenciabrasil.ebc.com.br/sites/default/files/example.jpg'
IBGE_ARTICLE = (
    'http://agenciadenoticias.ibge.gov.br/agencia-noticias/2012-agencia-de-noticias/'
    'noticias/41506-example.html'
)
IBGE_IMAGE = 'https://agenciadenoticias.ibge.gov.br/images/agenciadenoticias/ibge/example.jpg'
AVATAR = 'x_example_avatar'


def publication(record_id, source_id, identifier, url):
    return {
        'id': record_id,
        'x_studio_fontes': [source_id, 'Example'],
        'x_studio_identificador_externo': identifier,
        'x_studio_url': url,
        'x_studio_data_de_publicacao': '2024-10-09 01:58:00',
        'x_studio_assunto_1': [8, 'Example subject'],
        AVATAR: False,
    }


def source(record_id, code, method, url):
    return {
        'id': record_id,
        'x_studio_codigo_da_fonte_1': code,
        'x_studio_url': url,
        'x_studio_metodo_de_coleta': method,
        'x_active': True,
    }


class Odoo:
    def __init__(self, rows):
        self.rows = rows
        self.fail_on = None

    def avatar_field(self, *, write=False):
        return AVATAR

    def avatar_missing_count(self, field):
        return len(self.avatar_missing_page(field, after_id=0, limit=len(self.rows)))

    def avatar_missing_page(self, field, *, after_id, limit):
        return [row.copy() for row in self.rows if row['id'] > after_id and not row[field]][:limit]

    def fontes(self, *, include_archived=False):
        assert include_archived
        return [
            source(1, 'agencia_brasil', 'RSS', 'https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml'),
            source(2, 'camara', 'API REST', 'https://dadosabertos.camara.leg.br/api/v2/proposicoes'),
            source(3, 'ibge', 'API REST', 'https://servicodados.ibge.gov.br/api/v3/noticias'),
        ]

    def avatar_missing(self, publication_id, field):
        return any(row['id'] == publication_id and not row[field] for row in self.rows)

    def write_avatar(self, publication_id, field, encoded, *, expected):
        row = next(row for row in self.rows if row['id'] == publication_id)
        if publication_id == self.fail_on:
            raise RuntimeError('Odoo write was not confirmed')
        if row[field]:
            return False
        row[field] = encoded
        return True


class BackfillTests(TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            request_timeout=(10, 40), max_age_days=30, max_items_per_source=10,
            image_permissions={ARTICLE: IMAGE, IBGE_ARTICLE.replace('http:', 'https:'): IBGE_IMAGE},
        )
        self.session = object()
        self.old = publication(10, 1, ARTICLE, ARTICLE)
        self.odoo = Odoo([
            self.old,
            publication(11, 2, '42', 'https://dadosabertos.camara.leg.br/api/v2/proposicoes/42'),
            publication(12, 1, ARTICLE, ARTICLE) | {AVATAR: 'already-filled'},
        ])
        self.collect = self.enterContext(patch.object(module, 'coletar', return_value=[]))
        self.historic = self.enterContext(patch.object(module, 'image_reference', return_value=IMAGE))
        self.prepare = self.enterContext(patch.object(module, 'prepare_image', return_value='aW1hZ2U='))

    def test_preview_counts_old_news_and_skips_legislative_image_and_binary(self):
        result = module.backfill(self.cfg, self.odoo, self.session, apply=False, limit=10, after_id=0)
        self.assertEqual(result['missing'], 2)
        self.assertEqual(result['identified'], 1)
        self.assertEqual(result['without_image'], 1)
        self.assertEqual(result['eligible'], 1)
        self.assertEqual(result['updated'], 0)
        self.assertFalse(self.old[AVATAR])
        self.assertEqual(self.historic.call_count, 1)
        self.prepare.assert_not_called()

    def test_apply_resumes_using_cursor_and_only_writes_avatar(self):
        before = self.old.copy()
        first = module.backfill(self.cfg, self.odoo, self.session, apply=True, limit=1, after_id=0)
        self.assertEqual(first['next_after_id'], 10)
        self.assertEqual(first['updated'], 1)
        self.assertEqual(self.old, before | {AVATAR: 'aW1hZ2U='})
        second = module.backfill(self.cfg, self.odoo, self.session, apply=True, limit=10, after_id=10)
        self.assertEqual(second['missing'], 1)
        self.assertEqual(second['processed'], 1)
        self.assertIsNone(second['next_after_id'])
        self.assertEqual(self.odoo.rows[2][AVATAR], 'already-filled')
        self.assertEqual(self.prepare.call_count, 1)

    def test_missing_metadata_and_write_failure_leave_rows_available_for_retry(self):
        self.historic.return_value = None
        result = module.backfill(self.cfg, self.odoo, self.session, apply=True, limit=1, after_id=0)
        self.assertEqual(result['without_image'], 1)
        self.assertEqual(result['updated'], 0)
        self.historic.return_value = IMAGE
        self.odoo.fail_on = 10
        with redirect_stderr(StringIO()):
            result = module.backfill(self.cfg, self.odoo, self.session, apply=True, limit=1, after_id=0)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['retry_after_id'], 0)
        self.assertFalse(self.old[AVATAR])
        self.assertEqual(self.odoo.avatar_missing_count(AVATAR), 2)

    def test_invalid_bound_is_rejected(self):
        with self.assertRaises(ValueError):
            module.backfill(self.cfg, Mock(), self.session, apply=True, limit=0, after_id=0)

    def test_historical_ibge_metadata_matches_id_and_uses_local_publication_day(self):
        self.odoo.rows = [publication(20, 3, '41506', IBGE_ARTICLE)]
        endpoint = 'https://servicodados.ibge.gov.br/api/v3/noticias?qtd=100&de=10%2F08%2F2024&ate=10%2F08%2F2024'
        body = json.dumps({'items': [{
            'id': 41506,
            'link': IBGE_ARTICLE,
            'imagens': json.dumps({'image_fulltext': 'images/agenciadenoticias/ibge/example.jpg'}),
        }]}).encode()
        with patch.object(module, 'read_public', return_value=(body, endpoint)) as read:
            result = module.backfill(self.cfg, self.odoo, self.session, apply=False, limit=1, after_id=0)
        self.assertEqual(result['eligible'], 1)
        self.assertEqual(read.call_args.args, (endpoint,))
        self.assertEqual(read.call_args.kwargs['allowed_urls'], {endpoint})
        self.historic.assert_not_called()
        self.prepare.assert_not_called()

    def test_missing_permission_skips_publisher_requests(self):
        self.cfg.image_permissions.clear()
        result = module.backfill(self.cfg, self.odoo, self.session, apply=True, limit=10, after_id=0)
        self.assertEqual(result['without_image'], 1)
        self.assertEqual(result['permission_unverified'], 1)
        self.assertEqual(result['updated'], 0)
        self.collect.assert_not_called()
        self.historic.assert_not_called()
        self.prepare.assert_not_called()

    def test_overlapping_feed_cannot_associate_mismatched_url(self):
        self.collect.return_value = [
            module.Item(ARTICLE, 'An unrelated story', ARTICLE + '-different', None, None, 'Notícia', IMAGE),
        ]
        result = module.backfill(self.cfg, self.odoo, self.session, apply=True, limit=10, after_id=0)
        self.assertEqual(result['without_image'], 2)
        self.assertEqual(result['updated'], 0)
        self.historic.assert_not_called()
        self.prepare.assert_not_called()

    def test_time_budget_returns_the_last_processed_id(self):
        with patch.object(module, 'monotonic', side_effect=[0, 1, 300]):
            result = module.backfill(self.cfg, self.odoo, self.session, apply=True, limit=10, after_id=0)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['next_after_id'], 10)

    def test_avatar_filled_after_listing_avoids_download(self):
        def concurrent_avatar(*args, **kwargs):
            self.old[AVATAR] = 'filled-while-reading-metadata'
            return IMAGE

        self.historic.side_effect = concurrent_avatar
        result = module.backfill(self.cfg, self.odoo, self.session, apply=True, limit=10, after_id=0)
        self.assertEqual(result['updated'], 0)
        self.assertEqual(self.old[AVATAR], 'filled-while-reading-metadata')
        self.prepare.assert_not_called()
