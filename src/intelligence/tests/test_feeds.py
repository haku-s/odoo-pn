"""Feed parsing and publisher-specific image references."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest import TestCase
from unittest.mock import patch

from intelligence import network, sources


URL = 'https://www.camara.leg.br/noticias/rss/dinamico/SAUDE'
ARTICLE = 'https://www.camara.leg.br/noticias/123-health'
IMAGE = 'https://www.camara.leg.br/midias/image/photo.jpg'
SINCE = datetime(2026, 11, 1, tzinfo=UTC)


def rss(content='', *, date='Thu, 19 Nov 2026 10:00:00 GMT', link=ARTICLE):
    return f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"
        xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
        <item><title>Health</title><link>{link}</link><guid>one</guid>
            <pubDate>{date}</pubDate><description>&lt;p&gt;Summary&lt;/p&gt;</description>
            {content}
        </item></channel></rss>'''.encode()


def collect(data, url=URL):
    with patch.object(sources, 'read_public', return_value=(data, url)):
        return sources.coletar('health', None, url, SINCE, 20, method='RSS')


class SharedFeedTest(TestCase):
    def test_rss_no_image(self):
        item, = collect(rss())
        self.assertEqual((item.identificador, item.url, item.resumo), ('one', ARTICLE, 'Summary'))
        self.assertIsNone(item.image_url)

    def test_media_namespace_prefers_content(self):
        item, = collect(rss(f'<media:thumbnail url="{ARTICLE}/other"/>'
                            f'<media:content url="{IMAGE}" medium="image"/>'))
        self.assertEqual(item.image_url, IMAGE)

    def test_media_thumbnail_and_enclosure(self):
        for tag in (f'<media:thumbnail url="{IMAGE}"/>',
                    f'<enclosure url="{IMAGE}" type="image/jpeg"/>'):
            with self.subTest(tag=tag):
                item, = collect(rss(tag))
                self.assertEqual(item.image_url, IMAGE)

    def test_article_markup_and_nonimage_enclosure(self):
        item, = collect(rss(
            f'<enclosure url="{ARTICLE}/audio.mp3" type="audio/mpeg"/>'
            f'<content:encoded><![CDATA[<figure><img src="{IMAGE}"></figure>]]></content:encoded>'
        ))
        self.assertEqual(item.image_url, IMAGE)

    def test_atom_alternate_link_identity_offset_and_image(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>tag:example,2026:1</id><title>Health</title>
          <link rel="self" href="/feed.xml"/><link href="/noticias/123-health"/>
          <link rel="enclosure" type="image/jpeg" href="/midias/image/photo.jpg"/>
          <published>2026-11-19T08:00:00-03:00</published><summary>Summary</summary>
        </entry></feed>'''
        item, = collect(atom)
        self.assertEqual(item.identificador, 'tag:example,2026:1')
        self.assertEqual(item.url, ARTICLE)
        self.assertEqual(item.data_publicacao, '2026-11-19 11:00:00')
        self.assertEqual(item.image_url, IMAGE)

    def test_duplicate_items_keep_first_and_invalid_date_is_optional(self):
        data = rss(date='bad-date').decode()
        repeated = data[data.index('<item>'):data.index('</item>') + len('</item>')]
        item, = collect(data.replace('</channel>', repeated + '</channel>').encode())
        self.assertIsNone(item.data_publicacao)

    def test_old_entries_are_filtered(self):
        self.assertEqual(collect(rss(date='Sat, 31 Oct 2026 12:00:00 GMT')), [])

    def test_daily_feed_uses_sao_paulo_midnight_and_requires_a_date(self):
        start = datetime(2026, 11, 19, tzinfo=ZoneInfo('America/Sao_Paulo'))
        end = start + timedelta(days=1)
        cases = (
            ('Thu, 19 Nov 2026 02:59:59 GMT', False),
            ('Thu, 19 Nov 2026 03:00:00 GMT', True),
            ('Fri, 20 Nov 2026 02:59:59 GMT', True),
            ('Fri, 20 Nov 2026 03:00:00 GMT', False),
            ('bad-date', False),
            ('', False),
        )
        for date, accepted in cases:
            with self.subTest(date=date), patch.object(sources, 'read_public', return_value=(rss(date=date), URL)):
                items = sources.coletar('health', None, URL, start, 20, method='RSS', until=end)
                self.assertEqual(bool(items), accepted)

    def test_empty_feed(self):
        self.assertEqual(collect(b'<rss version="2.0"><channel/></rss>'), [])

    def test_malformed_nonfeed_and_dtd_responses(self):
        for body in (b'<rss', b'<html><body>Access denied</body></html>',
                     '<!DOCTYPE rss><rss><channel/></rss>'.encode('utf-16')):
            with self.subTest(body=body), self.assertRaises(sources.RespostaInvalida):
                collect(body)

    def test_disallowed_endpoint_never_downloads(self):
        with patch.object(sources, 'read_public') as read:
            with self.assertRaises(sources.FonteNaoImplementada):
                sources.coletar('health', None, URL + '?url=http://127.0.0.1', SINCE, 20, method='RSS')
            read.assert_not_called()

    def test_http_denial_redirect_and_oversized_feed_fail(self):
        scenarios = (
            (403, {}),
            (302, {'Location': 'https://127.0.0.1/feed'}),
            (200, {'Content-Length': str(sources.MAX_XML_BYTES + 1)}),
        )
        for status, headers in scenarios:
            with (
                self.subTest(status=status),
                patch.object(network, '_public_address', return_value='8.8.8.8'),
                patch.object(network, 'HTTPSConnectionPool') as pool,
            ):
                response = pool.return_value.urlopen.return_value
                response.status, response.headers = status, headers
                with self.assertRaises(sources.RespostaInvalida):
                    sources.collect_feed(URL, SINCE, 20, (2, 3))
                pool.assert_called_once()
                response.read1.assert_not_called()

    def test_image_off_publisher_host_is_ignored(self):
        item, = collect(rss('<media:thumbnail url="https://127.0.0.1/image.jpg"/>'))
        self.assertIsNone(item.image_url)


class ExistingPhotoTest(TestCase):
    def test_ebc_extracts_signed_image_and_preserves_link_identifier(self):
        url = 'https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml'
        link = 'https://agenciabrasil.ebc.com.br/saude/noticia/2026-11/news'
        image = 'https://agenciabrasil.ebc.com.br/sites/default/files/thumbnails/image/test.jpg'
        wrapped = f'https://imagens.ebc.com.br/{"a" * 27}=/1170x700/smart/{image}'
        markup = f'<imagem-destaque>{wrapped}</imagem-destaque>'
        item, = collect(rss(markup, link=link), url)
        self.assertEqual(item.identificador, link)
        self.assertEqual(item.image_url, wrapped)

    def test_ebc_proxy_rejects_untrusted_nested_url(self):
        prefix = f'https://imagens.ebc.com.br/{"a" * 27}=/1170x700/smart/'
        for original in (
            'https://127.0.0.1/sites/default/files/test.jpg',
            'https://agenciabrasil.ebc.com.br/sites/default/files/%252e%252e/test.jpg',
            'https://agenciabrasil.ebc.com.br/sites/default/files/test.jpg?url=private',
            'https://agenciabrasil.ebc.com.br@127.0.0.1/sites/default/files/test.jpg',
        ):
            with self.subTest(original=original):
                self.assertFalse(sources.is_ebc_image_proxy(prefix + original))

    def test_ibge_image_json_is_optional_and_resolves_against_publisher(self):
        relative = 'images/agenciadenoticias/ibge/test.jpg'
        metadata = '{"image_intro": "' + relative + '"}'
        self.assertEqual(sources.ibge_image_url(metadata), 'https://agenciadenoticias.ibge.gov.br/' + relative)
        for value in ('{bad', False, '{"image_intro":"//127.0.0.1/img.jpg"}'):
            with self.subTest(value=value):
                self.assertIsNone(sources.ibge_image_url(value))
