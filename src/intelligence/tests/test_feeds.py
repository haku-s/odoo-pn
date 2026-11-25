"""Feed parsing and publisher-specific image references."""

from datetime import UTC, datetime, timedelta
from collections import Counter
import json
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
    def test_ebc_thematic_channel_preserves_general_feed_identity(self):
        url = 'https://agenciabrasil.ebc.com.br/rss/saude/feed.xml'
        article = 'https://agenciabrasil.ebc.com.br/saude/noticia/2026-11/health'
        item, = collect(rss(link=article), url)
        self.assertEqual((sources.rss_publisher(url), item.identificador, item.url),
                         ('agenciabrasil.ebc.com.br', article, article))

    def test_feed_with_distinct_article_host_does_not_allow_that_host_as_a_redirect(self):
        url = 'https://feeds.bbci.co.uk/portuguese/rss.xml'
        address = 'https://www.bbc.com/portuguese/articles/test'
        body = rss(link=address)
        with patch.object(sources, 'read_public', return_value=(body, url)) as read:
            item, = sources.collect_feed(url, SINCE, 20, (2, 3))
        self.assertEqual(item.url, address)
        self.assertEqual(read.call_args.kwargs['allowed_urls'], {url})

    def test_fiocruz_recovers_an_older_article_through_approved_pagination(self):
        feed = 'https://agencia.fiocruz.br/rss.xml'
        archive = 'https://agencia.fiocruz.br/noticias'
        newer = 'https://agencia.fiocruz.br/new'
        markup = ('<div class="afn-noticias"><h3><a href="/old">Older article</a></h3>'
                  '<time datetime="2026-11-01T00:00:00Z"></time></div>').encode()
        audit = Counter()
        with patch.object(sources, 'read_public',
                          side_effect=[(rss(link=newer), feed), (markup, archive)]):
            items = sources.collect_news(feed, SINCE, 20, (2, 3), until=SINCE + timedelta(days=30), audit=audit)
        self.assertEqual([item.url for item in items], [newer, 'https://agencia.fiocruz.br/old'])
        self.assertEqual((audit['feed_window_unverified'], audit['archive_window_covered']), (1, 1))

    def test_archive_follows_only_expected_pagination_and_preserves_first_page_on_failure(self):
        url = 'https://agencia.fiocruz.br/noticias'
        markup = ('<div class="afn-noticias"><h3><a href="/news">Article</a></h3>'
                  '<time datetime="2026-11-19T10:00:00Z"></time></div>'
                  '<a href="https://127.0.0.1/news?page=1">Unapproved</a><a href="?page=1">Next</a>').encode()
        audit = Counter()
        with patch.object(sources, 'read_public', side_effect=[(markup, url), RuntimeError('unavailable')]) as read:
            items = sources.collect_news(url, SINCE, 20, (2, 3), audit=audit)
        self.assertEqual(len(items), 1)
        self.assertEqual([call.args[0] for call in read.call_args_list], [url, url + '?page=1'])
        self.assertEqual((audit['pagination_incomplete'], audit['archive_window_unverified']), (1, 1))

    def test_official_permalink_guid_supplies_the_link_for_health_and_senate(self):
        for url, article in (
            (sources.HEALTH_RSS, 'https://www.gov.br/saude/pt-br/assuntos/noticias/2026/novembro/health'),
            (sources.FEED_CHANNELS['senado_noticias'], 'https://www12.senado.leg.br/noticias/materias/2026/11/19/new'),
        ):
            with self.subTest(url=url):
                payload = rss(link=article).replace(f'<link>{article}</link><guid>one</guid>'.encode(),
                                                    f'<guid>{article}</guid>'.encode())
                item, = collect(payload, url)
                self.assertEqual((item.url, item.identificador), (article, article))

    def test_estadao_snapshot_uses_public_story_metadata_without_reading_article_pages(self):
        url = 'https://www.estadao.com.br/'
        body = b'''<script>Fusion.contentCache={"content-api":{"stories":[
          {"_id":"first","type":"story","canonical_url":"/economia/article-novo/",
           "headlines":{"basic":"Headline"},"subheadlines":{"basic":"Public summary"},
           "first_publish_date":"2026-11-19T10:00:00Z"}]}};
        </script>'''
        audit = Counter()
        with patch.object(sources, 'read_public', return_value=(body, url)) as read:
            item, = sources.collect_news(url, SINCE, 20, (2, 3), audit=audit)
        self.assertEqual((item.identificador, item.resumo), ('first', 'Public summary'))
        self.assertEqual(audit['archive_window_unverified'], 1)
        read.assert_called_once()

    def test_news_api_reconstructs_pagination_from_the_approved_route(self):
        url = sources.SENAI_API
        rows = [
            {'url': f'https://noticias.portaldaindustria.com.br/noticias/educacao/article-{number}/',
             'titulo': 'Headline', 'data_publicacao': '2026-11-19T10:00:00Z'}
            for number in range(20)
        ]
        first = json.dumps({'results': rows, 'count': 21, 'next': 'https://127.0.0.1/private'}).encode()
        second = json.dumps({'results': [{**rows[0], 'data_publicacao': '2026-11-01T00:00:00Z'}],
                             'count': 21, 'next': None}).encode()
        next_page = url.removesuffix('1') + '2'
        audit = Counter()
        with patch.object(sources, 'read_public', side_effect=[(first, url), (second, next_page)]) as read:
            items = sources.coletar('senai', None, url, SINCE, 25, method='API REST', audit=audit)
        self.assertEqual((len(items), audit['pages'], audit['source_duplicates']), (20, 2, 1))
        self.assertEqual(read.call_args.kwargs['allowed_urls'], {next_page})

    def test_invalid_entry_does_not_hide_a_valid_entry_and_audits_capacity(self):
        body = rss().replace(b'<item>', b'<item><title>Broken</title></item><item>', 1)
        body = body.replace(b'<channel>', b'<channel><atom:link xmlns:atom="http://www.w3.org/2005/Atom" rel="next" href="https://127.0.0.1/page"/>', 1)
        audit = Counter()
        with patch.object(sources, 'read_public', return_value=(body, URL)) as read:
            items = sources.collect_feed(URL, SINCE, 20, (2, 3), audit=audit)
        self.assertEqual([item.identificador for item in items], ['one'])
        self.assertEqual((audit['raw_entries'], audit['valid_items'], audit['invalid_items'],
                          audit['feed_next_unavailable']), (2, 1, 1, 1))
        read.assert_called_once()

    def test_feed_retries_one_transient_error_and_marks_an_unverified_window(self):
        audit = Counter()
        with (patch.object(sources, 'read_public', side_effect=[network.status_error(503, None), (rss(), URL)]) as read,
              patch.object(sources, 'sleep') as wait):
            items = sources.collect_feed(URL, SINCE, 20, (2, 3), audit=audit)
        self.assertEqual((len(items), read.call_count, audit['request_retries'], audit['feed_window_unverified']),
                         (1, 2, 1, 1))
        wait.assert_called_once_with(1)

    def test_out_of_range_timestamp_and_malformed_optional_photo_do_not_fail_feed(self):
        body = rss('<content:encoded><![CDATA[<div class="image-container" data-midia="1">'
                   '<span class="midia-creditos">Author / Câmara dos Deputados</span>'
                   '<img src="https://["></div>]]></content:encoded>').decode()
        item = body[body.index('<item>'):body.index('</item>') + len('</item>')]
        invalid = item.replace('Thu, 19 Nov 2026 10:00:00 GMT', '0001-01-01T00:00:00+14:00')
        body = body.replace(item, invalid + item).encode()
        audit = Counter()
        with patch.object(sources, 'read_public', return_value=(body, URL)):
            received, = sources.collect_feed(URL, SINCE, 20, (2, 3), until=SINCE + timedelta(days=30), audit=audit)
        self.assertEqual((received.identificador, audit['invalid_dates']), ('one', 1))
        self.assertIsNone(received.image_url)

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

    def test_camara_pairs_the_photo_with_its_own_credit(self):
        data = rss(
            f'<content:encoded><![CDATA[<div class="image-container" data-midia="1">'
            f'<div class="midia-creditos">Reuters</div><img src="{IMAGE}"></div>'
            f'<div class="image-container" data-midia="2"><div class="midia-creditos">'
            f'<em>João Silva / Câmara dos Deputados</em></div><img src="{IMAGE.replace("photo", "credited")}"></div>'
            ']]></content:encoded>'
        )
        item, = collect(data)
        self.assertEqual(item.image_url, IMAGE.replace('photo', 'credited'))
        self.assertEqual(item.image_credit, 'João Silva / Câmara dos Deputados')

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
                patch.object(network, '_public_addresses', return_value=['8.8.8.8']),
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
