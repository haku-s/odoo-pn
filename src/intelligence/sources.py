"""Source collectors for the interfaces that were verified as public and
compatible with the Publications model."""

from __future__ import annotations

from typing import Any

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from html.parser import HTMLParser
from time import sleep
from urllib.parse import urlencode, unquote, urljoin, urlsplit

import requests

from .camara_media import HOST as CAMARA_MEDIA_HOST, credited_photo
from .classify import fold
from .network import CGU_API_HOST, FetchError, read_public, read_public_response, status_error, validate_url
from .news_html import (
    ARCHIVES, EBC_ARCHIVE, FIOCRUZ_ARCHIVE, HEALTH_LISTING, HTML_CHANNELS, Listing,
    in_article_scope, listing_date, next_listing_page, parse_listing,
)

DOMINIO_OFICIAL = {
    'agencia_brasil': {'agenciabrasil.ebc.com.br'},
    'camara': {'dadosabertos.camara.leg.br'},
    'senado': {'legis.senado.leg.br'},
    'ibge': {'servicodados.ibge.gov.br'},
}

MAX_XML_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_PAGES = 20
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_CAMARA_INVALID_LOGS = 5

EBC_HOST = 'agenciabrasil.ebc.com.br'
EBC_IMAGE_HOST = 'imagens.ebc.com.br'
IBGE_ARTICLE_HOST = 'agenciadenoticias.ibge.gov.br'
EBC_GENERAL_FEEDS = {
    f'https://{EBC_HOST}/rss/ultimasnoticias/feed.xml',
    f'https://{EBC_HOST}/feed/ultimasnoticias/feed.xml',
}
EBC_CHANNELS = {
    f'agencia_brasil_{topic.replace("-", "_")}': f'https://{EBC_HOST}/rss/{topic}/feed.xml'
    for topic in ('saude', 'educacao', 'direitos-humanos', 'economia')
}
EBC_FEEDS = EBC_GENERAL_FEEDS | set(EBC_CHANNELS.values())
FEED_CHANNELS = {
    **EBC_CHANNELS,
    **{f'camara_{code}': f'https://www.camara.leg.br/noticias/rss/dinamico/{topic}'
       for code, topic in (
           ('ciencia_tecnologia', 'CIENCIA-E-TECNOLOGIA'), ('direitos_humanos', 'DIREITOS-HUMANOS'),
           ('administracao_publica', 'ADMINISTRACAO-PUBLICA'), ('assistencia_social', 'ASSISTENCIA-SOCIAL'),
           ('industria_comercio', 'INDUSTRIA-E-COMERCIO'),
       )},
    'jornal_usp': 'https://jornal.usp.br/feed/',
    'embrapii': 'https://embrapii.org.br/feed/',
    'sebrae': 'https://agenciasebrae.com.br/feed/',
    'poder360': 'https://www.poder360.com.br/feed/',
    'agencia_publica': 'https://apublica.org/feed/',
    'nexo': 'https://www.nexojornal.com.br/rss.xml',
    'valor': 'https://valor.globo.com/rss/valor',
    'exame': 'https://exame.com/feed/',
    'bbc_brasil': 'https://feeds.bbci.co.uk/portuguese/rss.xml',
    'g1': 'https://pox.globo.com/rss/g1/',
    'folha': 'https://feeds.folha.uol.com.br/emcimadahora/rss091.xml',
    'uol_noticias': 'https://rss.uol.com.br/feed/noticias.xml',
    'senado_noticias': 'https://www12.senado.leg.br/noticias/rss.xml',
}
RSS_PUBLISHERS = {
    **{url: urlsplit(url).netloc for url in FEED_CHANNELS.values()},
    **dict.fromkeys(EBC_GENERAL_FEEDS, EBC_HOST),
    FEED_CHANNELS['bbc_brasil']: 'www.bbc.com',
    FEED_CHANNELS['g1']: 'g1.globo.com',
    FEED_CHANNELS['folha']: 'www1.folha.uol.com.br',
    FEED_CHANNELS['uol_noticias']: 'noticias.uol.com.br',
    'https://www.camara.leg.br/noticias/rss/dinamico/EDUCACAO': 'www.camara.leg.br',
    'https://www.camara.leg.br/noticias/rss/dinamico/SAUDE': 'www.camara.leg.br',
    'https://www.camara.leg.br/noticias/rss/dinamico/TRABALHO': 'www.camara.leg.br',
    'https://agencia.fiocruz.br/rss.xml': 'agencia.fiocruz.br',
    'https://revistapesquisa.fapesp.br/feed/': 'revistapesquisa.fapesp.br',
    'https://www.gov.br/saude/pt-br/assuntos/noticias/noticias/atom.xml': 'www.gov.br',
    'https://www.gov.br/saude/pt-br/assuntos/noticias/noticias/rss.xml': 'www.gov.br',
    'https://portal.tcu.gov.br/rss.xml': 'portal.tcu.gov.br',
}
FUNDACENTRO_API = ('https://www.gov.br/fundacentro/++api++/pt-br/@search'
                   '?portal_type=News%20Item&sort_on=effective&sort_order=descending&b_size=20')
SENAI_API = 'https://noticias.portaldaindustria.com.br/api/noticias/?casas=4&limit=20&page=1'
NEWS_APIS = {'fundacentro': FUNDACENTRO_API, 'senai': SENAI_API}
ESTADAO_HOME = 'https://www.estadao.com.br/'
HEALTH_FEED = 'https://www.gov.br/saude/pt-br/assuntos/noticias/noticias/atom.xml'
HEALTH_RSS = 'https://www.gov.br/saude/pt-br/assuntos/noticias/noticias/rss.xml'
FIOCRUZ_FEED = 'https://agencia.fiocruz.br/rss.xml'
GUID_PERMALINK_FEEDS = {HEALTH_RSS, FEED_CHANNELS['senado_noticias']}
DIAGNOSTIC_CHANNELS = {
    **FEED_CHANNELS, **{code: listing.url for code, listing in HTML_CHANNELS.items()}, **NEWS_APIS,
    'agencia_brasil': f'https://{EBC_HOST}/rss/ultimasnoticias/feed.xml',
    'fiocruz': FIOCRUZ_FEED,
    'ministerio_saude': HEALTH_FEED,
    'camara_educacao': 'https://www.camara.leg.br/noticias/rss/dinamico/EDUCACAO',
    'camara_saude': 'https://www.camara.leg.br/noticias/rss/dinamico/SAUDE',
    'camara_trabalho': 'https://www.camara.leg.br/noticias/rss/dinamico/TRABALHO',
    'pesquisa_fapesp': 'https://revistapesquisa.fapesp.br/feed/',
    'tcu': 'https://portal.tcu.gov.br/rss.xml',
    'estadao': ESTADAO_HOME,
}
ALTERNATE_IDENTITY_URLS = frozenset((FIOCRUZ_FEED, FIOCRUZ_ARCHIVE.url, HEALTH_FEED, HEALTH_LISTING.url))
ATOM = '{http://www.w3.org/2005/Atom}'
MEDIA = '{http://search.yahoo.com/mrss/}'
CONTENT = '{http://purl.org/rss/1.0/modules/content/}encoded'
DC_DATE = '{http://purl.org/dc/elements/1.1/}date'
_EBC_PROXY = re.compile(
    r'https://imagens\.ebc\.com\.br/[A-Za-z0-9_-]{27}=/[1-9][0-9]{0,3}x[1-9][0-9]{0,3}/smart/'
    r'(https://agenciabrasil\.ebc\.com\.br/sites/default/files/[^?#]+(?:\?itok=[A-Za-z0-9_-]{1,128})?)'
)

TIPOS = {
    'agencia_brasil': 'Notícia',
    'camara': 'Matéria legislativa',
    'senado': 'Matéria legislativa',
    'ibge': 'Notícia',
}


class FonteNaoImplementada(RuntimeError):
    pass


class RespostaInvalida(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    identificador: str
    titulo: str
    url: str
    data_publicacao: str | None  # UTC '%Y-%m-%d %H:%M:%S'
    resumo: str | None
    tipo: str | None
    image_url: str | None = None
    image_credit: str | None = None


def _url_oficial(codigo: str, url: object) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError('Source URL is not configured')
    validate_url(url, DOMINIO_OFICIAL[codigo])
    return url


def _texto(value: object, limite: int = 4000) -> str | None:
    if not isinstance(value, str):
        return None
    texto = ' '.join(value.split())
    if not texto:
        return None
    return texto[:limite]


def _html_para_texto(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    texto = re.sub(r'<[^>]+>', ' ', value)
    texto = texto.replace('&nbsp;', ' ').replace('&amp;', '&')
    texto = ' '.join(texto.split())
    return texto[:4000] or None


def ebc_article_summary(url: str, timeout: tuple[int, int]) -> str | None:
    """Read the publisher's description when the news listing omits it."""
    validate_url(url, {EBC_HOST})
    if not in_article_scope(EBC_ARCHIVE, url):
        return None
    body, _ = read_public(url, allowed_urls={url}, max_bytes=512 * 1024, timeout=timeout)
    document = parse_listing(body)
    canonical = [el.attrs.get('href') for el in document.select('link') if el.attrs.get('rel') == 'canonical']
    if canonical != [url]:
        return None
    descriptions = [el.attrs.get('content') for el in document.select('meta')
                    if el.attrs.get('property') == 'og:description']
    return _texto(descriptions[0], 400) if len(descriptions) == 1 else None


def _data_hora(value: object) -> str | None:
    """Normalise source timestamps to UTC-naive Odoo datetimes."""
    if not isinstance(value, str) or not value.strip():
        return None
    brt = None
    try:
        from zoneinfo import ZoneInfo
        brt = ZoneInfo('America/Sao_Paulo')
    except Exception:  # pragma: no cover - platform without tzdata
        brt = None
    candidatos = []
    valor = value.strip()
    try:
        candidatos.append(parsedate_to_datetime(valor))
    except (TypeError, ValueError):
        pass
    try:
        candidatos.append(datetime.fromisoformat(valor))
    except ValueError:
        pass
    for formato in (
        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M', '%Y-%m-%d', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M',
        '%d/%m/%Y',
    ):
        try:
            # Naive source timestamps are assigned their source timezone below.
            candidatos.append(datetime.strptime(valor, formato))  # noqa: DTZ007
        except ValueError:
            continue
    for candidato in candidatos:
        if candidato is None:
            continue
        if candidato.tzinfo is None:
            if brt is not None:
                candidato = candidato.replace(tzinfo=brt)
            else:
                continue
        try:
            return candidato.astimezone(UTC).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')
        except (OverflowError, ValueError):
            continue
    raise RespostaInvalida('Unsupported publication date')


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime('%Y-%m-%d %H:%M:%S')


def _date_in_window(value: str | None, start: str, end: str | None) -> bool:
    if value is None:
        return end is None
    return value >= start and (end is None or value < end)


def _item_date(value, start: str, end: str | None, audit: Counter[str]):
    try:
        date = _data_hora(value)
    except RespostaInvalida:
        audit['invalid_dates'] += 1
        return False, None
    if date is None:
        audit['invalid_dates'] += 1
        if end is not None:
            return False, None
    audit['valid_items'] += 1
    accepted = _date_in_window(date, start, end)
    if not accepted:
        audit['outside_window'] += 1
    return accepted, date


def _api_has_next(data, page: int, batch_size: int, page_size: int, *, ibge: bool) -> bool:
    fallback = batch_size >= page_size
    if ibge:
        if 'page' in data and (type(data['page']) is not int or data['page'] != page):
            raise RespostaInvalida('API returned a different page than requested')
        if 'totalPages' in data:
            total = data['totalPages']
            if type(total) is not int or total < 0 or (batch_size and total < page):
                raise RespostaInvalida('Invalid API total pages')
            if 'nextPage' in data and (
                type(data['nextPage']) is not int or data['nextPage'] != (page + 1 if page < total else 0)
            ):
                raise RespostaInvalida('Invalid API next page')
            return page < total
    elif 'links' in data:
        links = data['links']
        if not isinstance(links, list) or not all(isinstance(link, dict) for link in links):
            raise RespostaInvalida('Invalid API pagination links')
        return any(link.get('rel') == 'next' for link in links)
    return fallback


def _retry_read(action, audit: Counter[str]):
    try:
        return action()
    except FetchError as exc:
        if not exc.retry:
            raise
        audit['request_retries'] += 1
        sleep(exc.delay)
    return action()


def _get(session: requests.Session, url: str, params: dict[str, Any], timeout,
         audit: Counter[str]) -> requests.Response:
    def read():
        try:
            response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            transient = not isinstance(exc, requests.exceptions.SSLError) and isinstance(
                exc, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError),
            )
            raise FetchError(f'Source request failed ({type(exc).__name__})', retry=transient) from exc
        if response.status_code != 200:
            raise status_error(response.status_code, response.headers.get('Retry-After'))
        return response

    try:
        return _retry_read(read, audit)
    except FetchError as exc:
        raise RespostaInvalida(str(exc)) from exc


def _json(response: requests.Response):
    try:
        return response.json()
    except ValueError as exc:
        raise RespostaInvalida('Invalid JSON response') from exc


# ------------------------------------------------------------------------- Feeds
def rss_publisher(url: object) -> str | None:
    return RSS_PUBLISHERS.get(url) if isinstance(url, str) else None


def news_publisher(url: object) -> str | None:
    if not isinstance(url, str) or (
        url not in RSS_PUBLISHERS and url not in ARCHIVES and url not in NEWS_APIS.values() and url != ESTADAO_HOME
    ):
        return None
    parts = urlsplit(url)
    if parts.netloc == 'www.gov.br':
        return f'{parts.netloc}/{parts.path.split("/")[1]}'
    return rss_publisher(url) or parts.netloc


def _linked_url(base: str, value: str | None, host: str) -> str | None:
    if not value or len(value) > 2000:
        return None
    try:
        target = urljoin(base, value.strip())
        validate_url(target, {host})
    except ValueError:
        return None
    return target


def is_ebc_image_proxy(url: str) -> bool:
    try:
        validate_url(url, {EBC_IMAGE_HOST})
    except ValueError:
        return False
    match = _EBC_PROXY.fullmatch(url)
    if not match:
        return False
    path = unquote(urlsplit(match[1]).path)
    return (
        not {'.', '..'}.intersection(path.split('/'))
        and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '%\\?#' for c in path)
        and path.lower().endswith(('.jpeg', '.jpg', '.png', '.webp'))
    )


class _ArticleImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag.rsplit(':', 1)[-1] == 'img':
            values = dict(attrs)
            url = values.get('src')
            if url and values.get('width') != '1' and values.get('height') != '1':
                self.urls.add(url)


def _field(element: ET.Element, name: str) -> str | None:
    node = element.find(name)
    if node is None:
        return None
    # Atom XHTML content carries real child elements rather than escaped markup.
    if len(node):
        return (node.text or '') + ''.join(ET.tostring(child, encoding='unicode') for child in node)
    return node.text


def _feed_image(element: ET.Element, article: str, host: str, *html: str | None) -> str | None:
    for node in element.iter(MEDIA + 'content'):
        if node.get('medium') == 'image' or node.get('type', '').lower().startswith('image/'):
            image = _linked_url(article, node.get('url'), host)
            if image:
                return image
    for node in element.iter(MEDIA + 'thumbnail'):
        image = _linked_url(article, node.get('url'), host)
        if image:
            return image
    for node in [*element.findall('enclosure'), *element.findall(ATOM + 'link')]:
        if node.tag == ATOM + 'link' and node.get('rel') != 'enclosure':
            continue
        if node.get('type', '').lower().startswith('image/'):
            image = _linked_url(article, node.get('url') or node.get('href'), host)
            if image:
                return image
    if host == EBC_HOST:
        featured = element.findtext('imagem-destaque', '').strip()
        if is_ebc_image_proxy(featured):
            return featured
        image = _linked_url(article, featured, host)
        return image if image and urlsplit(image).path.startswith('/sites/default/files/') else None
    for markup in html:
        if markup:
            parser = _ArticleImages()
            parser.feed(markup)
            if len(parser.urls) == 1:
                image = _linked_url(article, next(iter(parser.urls)), host)
                if image:
                    return image
    return None


def collect_feed(
    url: str, since: datetime, limit: int, timeout: tuple[int, int], *, until: datetime | None = None,
    audit: Counter[str] | None = None,
) -> list[Item]:
    counts = Counter() if audit is None else audit
    host = rss_publisher(url)
    if host is None:
        raise ValueError('Feed URL is not approved')
    try:
        endpoints = EBC_GENERAL_FEEDS if url in EBC_GENERAL_FEEDS else {url}
        body, final_url = _retry_read(
            lambda: read_public(url, allowed_urls=endpoints, max_bytes=MAX_XML_BYTES, timeout=timeout), counts,
        )
    except ValueError as exc:
        raise RespostaInvalida('Feed request destination is not approved') from exc
    except RuntimeError as exc:
        raise RespostaInvalida(str(exc)) from exc
    # Decode before checking for DTDs so alternate encodings cannot hide declarations.
    try:
        if url == FEED_CHANNELS['folha']:
            decoded = body.decode('iso-8859-1')
        elif url == FEED_CHANNELS['uol_noticias']:
            try:
                decoded = body.decode('utf-8-sig')
            except UnicodeDecodeError:
                decoded = body.decode('iso-8859-1')
        else:
            decoded = body.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise RespostaInvalida('Feed encoding is not UTF-8') from exc
    if '<!DOCTYPE' in decoded.upper():
        raise RespostaInvalida('Feed declares a DTD')
    try:
        root = ET.fromstring(decoded)
    except ET.ParseError as exc:
        raise RespostaInvalida('Invalid feed XML') from exc

    atom = root.tag == ATOM + 'feed'
    versions: set[str | None] = {'2.0', '0.91'} if url == FEED_CHANNELS['folha'] else {'2.0'}
    if url == FEED_CHANNELS['uol_noticias']:
        versions.add(None)
    channel = root.find('channel') if root.tag == 'rss' and root.get('version') in versions else None
    if atom:
        entries = root.findall(ATOM + 'entry')
    elif channel is not None:
        entries = channel.findall('item')
    else:
        raise RespostaInvalida('Unexpected feed format')

    counts['pages'] += 1
    counts['raw_entries'] += len(entries)
    metadata = root if atom else channel
    assert metadata is not None
    if any(link.get('rel') == 'next' for link in metadata.findall(ATOM + 'link')):
        # There is no approved pagination route for these feeds.
        counts['feed_next_unavailable'] += 1

    items = []
    identifiers: set[str] = set()
    links: set[str] = set()
    start = _utc_timestamp(since)
    end = _utc_timestamp(until) if until is not None else None
    reaches_start = False
    for entry in entries:
        if atom:
            address = next((link.get('href') for link in entry.findall(ATOM + 'link')
                            if link.get('rel', 'alternate') == 'alternate' and link.get('href')), None)
            title = _texto(_field(entry, ATOM + 'title'))
            identity = _texto(entry.findtext(ATOM + 'id'), 2000)
            date = entry.findtext(ATOM + 'published') or entry.findtext(ATOM + 'updated')
            summary = _field(entry, ATOM + 'summary')
            content = _field(entry, ATOM + 'content')
        else:
            address = entry.findtext('link')
            title = _texto(_field(entry, 'title'))
            identity = _texto(entry.findtext('guid'), 2000)
            if url in GUID_PERMALINK_FEEDS and not address:
                address = identity
            if url == FEED_CHANNELS['folha'] and address:
                redirect_prefix = 'https://redir.folha.com.br/redir/online/emcimadahora/rss091/*'
                address = address.removeprefix(redirect_prefix)
            date = entry.findtext('pubDate') or entry.findtext(DC_DATE)
            summary = _field(entry, 'description')
            content = _field(entry, CONTENT)
        link = _linked_url(final_url, address, host)
        if not link and url == FEED_CHANNELS['folha']:
            for section in ('c-level', 'f5', 'guia'):
                if link := _linked_url(final_url, address, f'{section}.folha.uol.com.br'):
                    break
        if link and url in (HEALTH_RSS, HEALTH_FEED) and not urlsplit(link).path.startswith('/saude/pt-br/'):
            link = None
        if link and url == FEED_CHANNELS['senado_noticias']:
            path = urlsplit(link).path
            if path.startswith('/noticias/audios/'):
                counts['non_news_entries'] += 1
                continue
            if not path.startswith('/noticias/materias/'):
                link = None
        if not link and address and url == FEED_CHANNELS['uol_noticias']:
            counts['syndicated_entries'] += 1
            continue
        if not title or not link:
            counts['invalid_items'] += 1
            continue
        identifier = link if host == EBC_HOST else identity or link
        try:
            published = _data_hora(date)
        except RespostaInvalida:
            published = None
        if published is None:
            counts['invalid_dates'] += 1
            if end is not None:
                continue
        else:
            reaches_start |= published <= start
        counts['valid_items'] += 1
        if not _date_in_window(published, start, end):
            counts['outside_window'] += 1
            continue
        if identifier in identifiers or link in links:
            counts['source_duplicates'] += 1
            continue
        identifiers.add(identifier)
        links.add(link)
        if len(items) >= limit:
            counts['item_limit_dropped'] += 1
            continue
        photo = None
        if host == CAMARA_MEDIA_HOST:
            try:
                photo = credited_photo(link, content or '') or credited_photo(link, summary or '')
            except ValueError:
                # A malformed optional photograph URL must not discard the article.
                pass
        items.append(Item(
            identificador=identifier, titulo=title, url=link, data_publicacao=published,
            resumo=_html_para_texto(summary if host == EBC_HOST else summary or content), tipo='Notícia',
            image_url=photo[0] if photo else _feed_image(entry, link, host, summary, content),
            image_credit=photo[1] if photo else None,
        ))
    if not reaches_start:
        counts['feed_window_unverified'] += 1
    return items


def _collect_listing(
    listing: Listing, since: datetime, limit: int, timeout: tuple[int, int],
    until: datetime | None, counts: Counter[str], items: list[Item],
) -> None:
    start = _utc_timestamp(since)
    end = _utc_timestamp(until) if until is not None else None
    host = urlsplit(listing.url).netloc
    seen = {item.url for item in items}
    url = listing.url
    signatures: set[tuple[str, ...]] = set()
    for page in range(MAX_ARCHIVE_PAGES):
        try:
            body, _ = _retry_read(
                partial(read_public, url, allowed_urls={url}, max_bytes=MAX_HTML_BYTES, timeout=timeout), counts,
            )
            document = parse_listing(body)
            rows = list(document.select(listing.rows))
            if not rows:
                raise RespostaInvalida('News listing has no recognized article rows')
        except (ValueError, RuntimeError) as exc:
            if not items and page == 0:
                raise RespostaInvalida(f'News listing failed: {exc}') from exc
            counts['pagination_incomplete'] += 1
            counts['archive_errors'] += 1
            break

        counts['pages'] += 1
        counts['raw_entries'] += len(rows)
        page_dates = []
        page_links = []
        for row in rows:
            title_el = row.first(listing.title)
            link_el = row if row.tag == 'a' else title_el if title_el and title_el.tag == 'a' else row.first(listing.link)
            if title_el and listing.link == 'a' and title_el.tag != 'a':
                link_el = title_el.first('a') or link_el
            title = _texto(title_el.text()) if title_el else None
            link = _linked_url(url, link_el.attrs.get('href'), host) if link_el else None
            if listing == HTML_CHANNELS['agencia_bndes'] and link and urlsplit(link).path.startswith('/pautas-bndes/'):
                counts['non_news_entries'] += 1
                continue
            if not title or not link or not in_article_scope(listing, link):
                counts['invalid_items'] += 1
                continue
            page_links.append(link)
            date = listing_date(row.first(listing.date))
            try:
                published = _data_hora(date)
            except RespostaInvalida:
                published = None
            if not published:
                counts['invalid_dates'] += 1
                continue
            page_dates.append(published)
            counts['valid_items'] += 1
            # Date-only listings report a local calendar day. Include its overlap with the window.
            if date and len(date) == 10:
                next_day = datetime.strptime(published, '%Y-%m-%d %H:%M:%S') + timedelta(days=1)  # noqa: DTZ007
                in_window = next_day.strftime('%Y-%m-%d %H:%M:%S') > start and (end is None or published < end)
            else:
                in_window = _date_in_window(published, start, end)
            if not in_window:
                counts['outside_window'] += 1
                continue
            if link in seen:
                counts['source_duplicates'] += 1
                continue
            seen.add(link)
            if len(items) >= limit:
                counts['item_limit_dropped'] += 1
                continue
            summary = row.first(listing.summary)
            text = summary.text() if summary else ''
            if text.startswith((date or '') + ' - '):
                text = text[len(date or '') + 3:]
            image, credit = None, None
            if listing in (EBC_ARCHIVE, FIOCRUZ_ARCHIVE) and (photo := row.first('img')):
                candidate = photo.attrs.get('data-echo') or photo.attrs.get('src')
                image = candidate if candidate and is_ebc_image_proxy(candidate) else _linked_url(link, candidate, host)
                credit = _texto(photo.attrs.get('title'), 300)
            items.append(Item(link, title, link, published, _texto(text), 'Notícia', image, credit))
        signature = tuple(page_links)
        if not signature or signature in signatures:
            counts['pagination_incomplete'] += 1
            break
        signatures.add(signature)
        if page_dates and min(page_dates) <= start:
            counts['archive_window_covered'] += 1
            return
        next_url = next_listing_page(listing, document, url, (page + listing.first_next) * listing.step)
        if not next_url or counts['item_limit_dropped']:
            break
        url = next_url
    else:
        counts['pagination_incomplete'] += 1
    counts['archive_window_unverified'] += 1


def _collect_news_api(url: str, since: datetime, limit: int, timeout: tuple[int, int],
                      until: datetime | None, counts: Counter[str]) -> list[Item]:
    fundacentro = url == FUNDACENTRO_API
    start = _utc_timestamp(since)
    end = _utc_timestamp(until) if until is not None else None
    host = urlsplit(url).netloc
    items, seen, signatures = [], set(), set()
    for page in range(MAX_ARCHIVE_PAGES):
        address = (url if page == 0 else url + f'&b_start={page * 20}') if fundacentro else (
            url.rsplit('=', 1)[0] + f'={page + 1}'
        )
        try:
            body, _ = _retry_read(
                partial(read_public, address, allowed_urls={address}, max_bytes=MAX_HTML_BYTES, timeout=timeout), counts,
            )
            data = json.loads(body)
            rows = data.get('items' if fundacentro else 'results') if isinstance(data, dict) else None
            if not isinstance(rows, list) or len(rows) > 20:
                raise RespostaInvalida('Unexpected news API response')
        except (ValueError, RuntimeError) as exc:
            if page == 0:
                raise RespostaInvalida(f'News API failed: {exc}') from exc
            counts['pagination_incomplete'] += 1
            break
        counts['pages'] += 1
        counts['raw_entries'] += len(rows)
        dates, links = [], []
        for row in rows:
            if not isinstance(row, dict):
                counts['invalid_items'] += 1
                continue
            title = _texto(row.get('title' if fundacentro else 'titulo'))
            ref = _texto(row.get('@id' if fundacentro else 'url'), 2000)
            link = _linked_url(url, ref, host)
            path = urlsplit(link).path if link else ''
            correct_scope = path.startswith('/fundacentro/pt-br/comunicacao/noticias/' if fundacentro else '/noticias/')
            if not title or not link or not correct_scope:
                counts['invalid_items'] += 1
                continue
            links.append(link)
            try:
                published = _data_hora(row.get('effective' if fundacentro else 'data_publicacao'))
            except RespostaInvalida:
                published = None
            if not published:
                counts['invalid_dates'] += 1
                continue
            dates.append(published)
            counts['valid_items'] += 1
            if not _date_in_window(published, start, end):
                counts['outside_window'] += 1
                continue
            if link in seen:
                counts['source_duplicates'] += 1
                continue
            seen.add(link)
            if len(items) >= limit:
                counts['item_limit_dropped'] += 1
                continue
            summary = _html_para_texto(row.get('description' if fundacentro else 'lead'))
            items.append(Item(link, title, link, published, summary, 'Notícia'))
        signature = tuple(links)
        if signature in signatures or not links and rows:
            counts['pagination_incomplete'] += 1
            break
        signatures.add(signature)
        if dates and min(dates) <= start:
            counts['archive_window_covered'] += 1
            return items
        total = data.get('items_total' if fundacentro else 'count')
        if type(total) is not int or total < page * 20 + len(rows) or not rows and total > page * 20:
            counts['pagination_incomplete'] += 1
            break
        if total <= (page + 1) * 20 or counts['item_limit_dropped']:
            break
        if len(rows) != 20:
            counts['pagination_incomplete'] += 1
            break
    else:
        counts['pagination_incomplete'] += 1
    counts['archive_window_unverified'] += 1
    return items


def _collect_arc_snapshot(since: datetime, limit: int, timeout: tuple[int, int],
                          until: datetime | None, counts: Counter[str]) -> list[Item]:
    try:
        body, _ = _retry_read(partial(read_public, ESTADAO_HOME, allowed_urls={ESTADAO_HOME},
                                      max_bytes=MAX_HTML_BYTES, timeout=timeout), counts)
        _, marker, public_data = body.decode('utf-8-sig').partition('Fusion.contentCache=')
        if not marker:
            raise ValueError('Missing public story metadata')
        cache, _ = json.JSONDecoder().raw_decode(public_data.lstrip())
    except (ValueError, RuntimeError) as exc:
        if isinstance(exc, FetchError):
            raise RespostaInvalida(str(exc)) from exc
        raise RespostaInvalida('Invalid public story metadata') from exc
    if not isinstance(cache, dict) or 'content-api' not in cache:
        raise RespostaInvalida('Missing public story collection')
    pending = [cache['content-api']]
    items, urls = [], set()
    start = _utc_timestamp(since)
    end = _utc_timestamp(until) if until is not None else None
    counts['pages'] += 1
    counts['archive_window_unverified'] += 1  # Publisher homepage selection has no archive cursor.
    while pending:
        row = pending.pop()
        if isinstance(row, list):
            pending.extend(row)
            continue
        if not isinstance(row, dict):
            continue
        if row.get('type') != 'story':
            pending.extend(row.values())
            continue
        counts['raw_entries'] += 1
        titles = row.get('headlines')
        title = _texto(titles.get('basic')) if isinstance(titles, dict) else None
        link = _linked_url(ESTADAO_HOME, _texto(row.get('canonical_url'), 2000), 'www.estadao.com.br')
        identity = _texto(row.get('_id'), 2000)
        if not title or not link or not identity:
            counts['invalid_items'] += 1
            continue
        try:
            published = _data_hora(row.get('first_publish_date'))
        except RespostaInvalida:
            published = None
        if not published:
            counts['invalid_dates'] += 1
            continue
        counts['valid_items'] += 1
        if not _date_in_window(published, start, end):
            counts['outside_window'] += 1
            continue
        if link in urls:
            counts['source_duplicates'] += 1
            continue
        urls.add(link)
        summaries = row.get('subheadlines')
        summary = _html_para_texto(summaries.get('basic')) if isinstance(summaries, dict) else None
        items.append(Item(identity, title, link, published, summary, 'Notícia'))
    if not counts['raw_entries']:
        raise RespostaInvalida('Public story collection has no entries')
    items.sort(key=lambda item: item.data_publicacao or '', reverse=True)
    if len(items) > limit:
        counts['item_limit_dropped'] += len(items) - limit
    return items[:limit]


def collect_news(url: str, since: datetime, limit: int, timeout: tuple[int, int], *,
                 until: datetime | None = None, audit: Counter[str] | None = None) -> list[Item]:
    counts = Counter() if audit is None else audit
    if url in NEWS_APIS.values():
        return _collect_news_api(url, since, limit, timeout, until, counts)
    if url == ESTADAO_HOME:
        return _collect_arc_snapshot(since, limit, timeout, until, counts)
    if url == HEALTH_FEED or url in ARCHIVES:
        items: list[Item] = []
        try:
            _collect_listing(HEALTH_LISTING if url == HEALTH_FEED else ARCHIVES[url],
                             since, limit, timeout, until, counts, items)
        except RespostaInvalida:
            if url != HEALTH_FEED:
                raise
            counts['archive_errors'] += 1
            try:
                return collect_feed(HEALTH_RSS, since, limit, timeout, until=until, audit=counts)
            except RespostaInvalida:
                counts['feed_errors'] += 1
                return collect_feed(url, since, limit, timeout, until=until, audit=counts)
        return items
    archive = EBC_ARCHIVE if url in EBC_GENERAL_FEEDS else FIOCRUZ_ARCHIVE if url == FIOCRUZ_FEED else None
    try:
        items = collect_feed(url, since, limit, timeout, until=until, audit=counts)
    except RespostaInvalida:
        if archive is None:
            raise
        counts['feed_errors'] += 1
        items = []
        _collect_listing(archive, since, limit, timeout, until, counts, items)
        return items
    if counts['feed_window_unverified']:
        if archive:
            try:
                _collect_listing(archive, since, limit, timeout, until, counts, items)
            except RespostaInvalida:
                counts['archive_errors'] += 1
                counts['pagination_incomplete'] += 1
                counts['archive_window_unverified'] += 1
    return items


def coletar_agencia_brasil(session, url, desde, limite, *, until: datetime | None = None, audit=None):
    if rss_publisher(url) != EBC_HOST:
        raise ValueError('Feed URL is not approved for this source')
    return collect_news(url, desde, limite, (10, 40), until=until, audit=audit)


# ----------------------------------------------------------------------- Câmara
def coletar_camara(session, url, desde, limite, *, until: datetime | None = None, audit=None):
    counts = Counter() if audit is None else audit
    url = _url_oficial('camara', url)
    start = _utc_timestamp(desde)
    end = _utc_timestamp(until) if until is not None else None
    pagina = 1
    itens = []
    identificadores_vistos = set()
    identificadores_aceitos = set()
    invalidos = 0
    validos = 0
    primeiro_invalido = None
    logger = logging.getLogger(__name__)
    # Keep a constant page size for the whole pagination: changing it between
    # requests shifts the offset and makes the API repeat earlier records.
    tamanho_pagina = min(100, limite)
    while len(itens) < limite:
        dados = _json(_get(session, url, {
            'dataInicio': desde.strftime('%Y-%m-%d'),
            'dataFim': (desde if until is not None else datetime.now(UTC).astimezone()).strftime('%Y-%m-%d'),
            'ordenarPor': 'id',
            'ordem': 'DESC',
            'itens': tamanho_pagina,
            'pagina': pagina,
        }, (10, 40), counts))
        if not isinstance(dados, dict) or not isinstance(dados.get('dados'), list):
            raise RespostaInvalida('Invalid proposition response')
        lote = dados['dados']
        counts['pages'] += 1
        counts['raw_entries'] += len(lote)
        more = _api_has_next(dados, pagina, len(lote), tamanho_pagina, ibge=False)
        if len(lote) > tamanho_pagina:
            counts['pagination_incomplete'] += 1
        if not lote:
            counts['pagination_incomplete'] += int(more)
            break
        antes = len(identificadores_vistos)
        for registro in lote:
            identificador = registro.get('id') if isinstance(registro, dict) else None
            try:
                if type(identificador) is not int or identificador <= 0:
                    raise RespostaInvalida('Missing or invalid proposition identifier')
                if identificador in identificadores_aceitos:
                    # Keep the first valid record. An invalid earlier occurrence
                    # must not suppress a later corrected record with the same ID.
                    counts['source_duplicates'] += 1
                    continue
                identificadores_vistos.add(identificador)
                ementa = _texto(registro.get('ementa'))
                uri = _texto(registro.get('uri'), 2000)
                if not ementa:
                    raise RespostaInvalida('Missing proposition summary')
                if not uri:
                    raise RespostaInvalida('Missing proposition URL')
                numero = registro.get('numero')
                if type(numero) is not int or numero <= 0:
                    raise RespostaInvalida('Invalid proposition number')
            except RespostaInvalida as exc:
                invalidos += 1
                counts['invalid_items'] += 1
                if primeiro_invalido is None:
                    primeiro_invalido = str(exc)
                if invalidos <= MAX_CAMARA_INVALID_LOGS:
                    logger.warning(
                        'Source camara: skipped invalid proposition page=%d id=%s reason=%s',
                        pagina, identificador if type(identificador) is int else 'unknown', exc,
                    )
                continue

            accepted, data = _item_date(registro.get('dataApresentacao'), start, end, counts)
            if data is not None or accepted:
                identificadores_aceitos.add(identificador)
                validos += 1
            if not accepted:
                continue
            if len(itens) >= limite:
                counts['item_limit_reached'] = 1
                continue
            sigla = _texto(registro.get('siglaTipo')) or 'PROPOSIÇÃO'
            ano = registro.get('ano')
            titulo = f'{sigla} {numero}/{ano}' if type(ano) is int and ano > 0 else f'{sigla} {numero}'
            itens.append(Item(
                identificador=str(identificador),
                titulo=titulo,
                url=uri,
                data_publicacao=data,
                resumo=ementa,
                tipo=TIPOS['camara'],
            ))
        if more and len(itens) >= limite:
            counts['item_limit_reached'] = 1
        if len(identificadores_vistos) == antes:
            counts['pagination_incomplete'] += 1
            break
        if not more:
            break
        pagina += 1
        if pagina > 50:
            counts['pagination_incomplete'] += 1
            break
    if invalidos:
        if validos == 0:
            raise RespostaInvalida(
                f'No valid proposition records ({invalidos} invalid; first: {primeiro_invalido})'
            )
        logger.warning(
            'Source camara: skipped_invalid_propositions=%d valid_propositions=%d',
            invalidos, validos,
        )
    return itens


# ---------------------------------------------------------------------- Senado
def coletar_senado(session, url, desde, limite, *, until: datetime | None = None, audit=None):
    counts = Counter() if audit is None else audit
    url = _url_oficial('senado', url)
    start = _utc_timestamp(desde)
    end = _utc_timestamp(until) if until is not None else None
    dados = _json(_get(session, url, {
        'dataInicioApresentacao': desde.strftime('%Y-%m-%d'),
        'dataFimApresentacao': (desde if until is not None else datetime.now(UTC).astimezone()).strftime('%Y-%m-%d'),
    }, (10, 60), counts))
    if not isinstance(dados, list):
        raise RespostaInvalida('Invalid process response')
    itens = []
    seen = set()
    counts['pages'] += 1
    counts['raw_entries'] += len(dados)
    for registro in dados:
        if not isinstance(registro, dict):
            counts['invalid_items'] += 1
            continue
        identificador = registro.get('codigoMateria')
        ementa = _texto(registro.get('ementa'))
        if type(identificador) is not int or identificador <= 0 or not ementa:
            counts['invalid_items'] += 1
            continue
        if identificador in seen:
            counts['source_duplicates'] += 1
            continue
        accepted, data = _item_date(registro.get('dataApresentacao'), start, end, counts)
        if not accepted:
            continue
        seen.add(identificador)
        if len(itens) >= limite:
            counts['item_limit_reached'] = 1
            continue
        identificacao = _texto(registro.get('identificacao')) or str(identificador)
        url_documento = _texto(registro.get('urlDocumento'), 2000)
        if not url_documento:
            # Documented record-specific URL for the Senate open-data service.
            url_documento = (
                'https://www25.senado.leg.br/web/atividade/materias/-/materia/'
                f'{identificador}'
            )
        itens.append(Item(
            identificador=str(identificador),
            titulo=identificacao,
            url=url_documento,
            data_publicacao=data,
            resumo=ementa,
            tipo=TIPOS['senado'],
        ))
    return itens


# ------------------------------------------------------------------------ IBGE
def ibge_image_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        metadata = json.loads(value)
    except ValueError:
        return None
    if not isinstance(metadata, dict):
        return None
    for key in ('image_intro', 'image_fulltext'):
        path = metadata.get(key)
        if not isinstance(path, str):
            continue
        target = _linked_url(f'https://{IBGE_ARTICLE_HOST}/', path, IBGE_ARTICLE_HOST)
        if target and urlsplit(target).path.startswith('/images/agenciadenoticias/'):
            return target
    return None


def coletar_ibge(session, url, desde, limite, *, until: datetime | None = None, audit=None,
                 non_news_window: tuple[datetime, datetime] | None = None, news_only: bool = False):
    counts = Counter() if audit is None else audit
    url = _url_oficial('ibge', url)
    start = _utc_timestamp(desde)
    end = _utc_timestamp(until) if until is not None else None
    pagina = 1
    itens = []
    identificadores_vistos = set()
    last_metadata = None
    # Keep a constant page size for the whole pagination.
    tamanho_pagina = min(100, limite)
    date_params = {}
    if until is not None:
        last_day = (until - timedelta(microseconds=1)).date()
        date_params = {'de': desde.strftime('%m/%d/%Y'), 'ate': last_day.strftime('%m/%d/%Y')}
    other_start, other_end = tuple(map(_utc_timestamp, non_news_window)) if non_news_window else (start, end)
    while len(itens) < limite:
        dados = _json(_get(session, url, {
            # The official API uses `qtd` for the page size; `quantidade` is
            # not recognised and silently falls back to the default.
            'qtd': tamanho_pagina,
            'page': pagina,
            **date_params,
        }, (10, 40), counts))
        if not isinstance(dados, dict) or not isinstance(dados.get('items'), list):
            raise RespostaInvalida('Invalid news response')
        lote = dados['items']
        counts['pages'] += 1
        counts['raw_entries'] += len(lote)
        more = _api_has_next(dados, pagina, len(lote), tamanho_pagina, ibge=True)
        metadata = (dados.get('count'), dados.get('totalPages')) if 'totalPages' in dados else None
        if ((last_metadata is not None and metadata != last_metadata) or len(lote) > tamanho_pagina
                or (metadata is not None and (type(metadata[0]) is not int or metadata[0] < 0))):
            counts['pagination_incomplete'] += 1
        if metadata is not None and type(metadata[0]) is int and not more and counts['raw_entries'] != metadata[0]:
            counts['pagination_incomplete'] += 1
        last_metadata = metadata
        if not lote:
            counts['pagination_incomplete'] += int(more)
            break
        duplicates_before = counts['source_duplicates']
        for registro in lote:
            if not isinstance(registro, dict):
                counts['invalid_items'] += 1
                continue
            identificador = registro.get('id')
            titulo = _texto(registro.get('titulo'))
            link = _texto(registro.get('link'), 2000)
            if type(identificador) is not int or identificador <= 0 or not titulo or not link:
                counts['invalid_items'] += 1
                continue
            if identificador in identificadores_vistos:
                counts['source_duplicates'] += 1
                continue
            tipo = TIPOS['ibge'] if fold(registro.get('tipo')) == 'noticia' else None
            if news_only and tipo is None:
                identificadores_vistos.add(identificador)
                counts['outside_replay_scope'] += 1
                continue
            low, high = (start, end) if tipo else (other_start, other_end)
            accepted, data = _item_date(registro.get('data_publicacao'), low, high, counts)
            if data is not None or accepted:
                identificadores_vistos.add(identificador)
            if not accepted:
                continue
            if len(itens) >= limite:
                counts['item_limit_reached'] = 1
                continue
            itens.append(Item(
                identificador=str(identificador),
                titulo=titulo,
                url=link,
                data_publicacao=data,
                resumo=_texto(registro.get('introducao')),
                tipo=tipo,
                image_url=ibge_image_url(registro.get('imagens')),
            ))
        if more and len(itens) >= limite:
            counts['item_limit_reached'] = 1
        if counts['source_duplicates'] - duplicates_before == len(lote):
            counts['pagination_incomplete'] += 1
            break
        if not more:
            break
        pagina += 1
        if pagina > 50:
            counts['pagination_incomplete'] += 1
            break
    return itens


_BCB_SERIES = 'https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados'
_BCB_NO_OBSERVATION = 'br.gov.bcb.pec.sgs.comum.excecoes.SGSNegocioException: Value(s) not found'


def coletar_bcb(session, url, desde, limite, *, until: datetime | None = None, audit=None) -> list[Item]:
    counts = Counter() if audit is None else audit
    if not isinstance(url, str) or url.removesuffix('?formato=json') not in (_BCB_SERIES, _BCB_SERIES + '/ultimos/3'):
        raise ValueError('Banco Central source does not match the configured SGS series')
    day = desde.strftime('%d/%m/%Y')
    address = _BCB_SERIES + '?' + urlencode({'formato': 'json', 'dataInicial': day, 'dataFinal': day})
    raw, _, status = _retry_read(
        lambda: read_public_response(
            address, allowed_urls={address}, max_bytes=32 * 1024, timeout=(5, 12), accepted_statuses=(200, 404),
        ), counts,
    )
    counts['pages'] += 1
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise RespostaInvalida('Invalid Banco Central response format') from exc
    if status == 404:
        error = payload.get('erro') if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get('statusCode') == 404 and error.get('detail') == _BCB_NO_OBSERVATION:
            return []
        raise RespostaInvalida('Banco Central returned an unrecognized HTTP 404 response')
    if payload == []:
        return []
    if isinstance(payload, list):
        counts['raw_entries'] += len(payload)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RespostaInvalida('Invalid Banco Central observation')
    value = payload[0].get('valor')
    if payload[0].get('data') != day or not isinstance(value, str) or not re.fullmatch(r'-?[0-9]{1,30}(?:\.[0-9]{1,30})?', value):
        raise RespostaInvalida('Invalid Banco Central observation date or value')
    counts['valid_items'] += 1
    return [Item(
        identificador=f'12:{desde.date().isoformat()}',
        titulo='Banco Central — série SGS 12',
        url=address,
        data_publicacao=None,
        resumo=f'SGS 12 — data de referência: {day}. Valor: {value}.',
        tipo='Divulgação estatística',
    )]


CGU_SOURCE = 'portal_transparencia'
CGU_BASE_URL = f'https://{CGU_API_HOST}'
CGU_MAX_PAGES = 4


def coletar_cgu(
    session: requests.Session | None, url: object, desde: datetime, limite: int, *,
    until: datetime | None = None, audit: Counter[str] | None = None,
) -> list[Item]:
    """Scan bounded CGU results and select records by official publication date."""
    if url not in (CGU_BASE_URL, CGU_BASE_URL + '/'):
        raise FonteNaoImplementada('CGU API origin is not approved')
    token = os.getenv('CGU_API_TOKEN', '').strip()
    if not token:
        raise RespostaInvalida('Missing CGU_API_TOKEN')
    codes = tuple(dict.fromkeys(code.strip() for code in os.getenv('CGU_SIAFI_CODES', '').split(',')))
    if not 1 <= len(codes) <= 8 or any(not re.fullmatch(r'[0-9]{5}', code) for code in codes):
        raise RespostaInvalida('CGU_SIAFI_CODES must contain one to eight comma-separated SIAFI codes')
    audit = audit if audit is not None else Counter()
    # The API's range parameters refer to opening or validity and may hide publications in this window.
    audit['feed_window_unverified'] += 1
    start = _utc_timestamp(desde.replace(hour=0, minute=0, second=0, microsecond=0))
    end = _utc_timestamp(until) if until is not None else None
    items: dict[str, Item] = {}
    failure = None
    for code in codes:
        for path, date_key, label, kind in (
            ('licitacoes', 'dataPublicacao', 'Licitação', 'Ato oficial'),
            ('contratos', 'dataPublicacaoDOU', 'Contrato', 'Contrato público'),
        ):
            for page in range(1, CGU_MAX_PAGES + 1):
                query = urlencode({'codigoOrgao': code, 'pagina': page})
                address = f'{CGU_BASE_URL}/api-de-dados/{path}?{query}'
                try:
                    def read(address: str = address) -> tuple[bytes, str, int]:
                        return read_public_response(address, allowed_urls={address}, max_bytes=2 * 1024 * 1024,
                                                    timeout=(5, 10), cgu_token=token)

                    body, _, _ = _retry_read(read, audit)
                    rows: object = json.loads(body)
                    if not isinstance(rows, list):
                        raise RespostaInvalida('CGU response is not an array')
                except (RuntimeError, ValueError) as exc:
                    audit['feed_errors'] += 1
                    if isinstance(exc, FetchError) and str(exc).endswith(('HTTP 401', 'HTTP 403')):
                        raise
                    failure = exc if isinstance(exc, FetchError) else RespostaInvalida('CGU response could not be read')
                    break
                audit['pages'] += 1
                if not rows:
                    break
                audit['raw_entries'] += len(rows)
                for row in rows:
                    if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
                        audit['invalid_items'] += 1
                        continue
                    purchase = row.get('licitacao') if path == 'licitacoes' else row
                    description = _texto(purchase.get('objeto')) if isinstance(purchase, dict) else None
                    if not description:
                        audit['invalid_items'] += 1
                        continue
                    published = _data_hora(row.get(date_key))
                    if published is None:
                        audit['invalid_dates'] += 1
                        continue
                    audit['valid_items'] += 1
                    if not _date_in_window(published, start, end):
                        audit['outside_window'] += 1
                        continue
                    identifier = f'{path}:{row["id"]}'
                    if identifier in items:
                        audit['source_duplicates'] += 1
                        continue
                    link = f'https://portaldatransparencia.gov.br/{path}/{row["id"]}'
                    items[identifier] = Item(identifier, f'{label} — {description}'[:240], link,
                                             published, description, kind)
    if failure is not None and not audit['pages']:
        raise failure
    return sorted(items.values(), key=lambda item: item.data_publicacao or '', reverse=True)[:limite]


COLETORES = {
    'agencia_brasil': coletar_agencia_brasil,
    'camara': coletar_camara,
    'senado': coletar_senado,
    'ibge': coletar_ibge,
    'bcb_sgs': coletar_bcb,
    CGU_SOURCE: coletar_cgu,
}


def coletar(
    codigo: str, session, url, desde, limite, *, method=None, timeout=(10, 40), until: datetime | None = None,
    audit: Counter[str] | None = None, non_news_window: tuple[datetime, datetime] | None = None,
    news_only: bool = False,
) -> list[Item]:
    is_news_api = method == 'API REST' and NEWS_APIS.get(codigo) == url
    if codigo == 'agencia_brasil' or (method == 'RSS' and codigo not in COLETORES) or is_news_api:
        publisher = news_publisher(url)
        if publisher is None or (codigo == 'agencia_brasil' and publisher != EBC_HOST) or (
            url in NEWS_APIS.values() and not is_news_api
        ):
            raise FonteNaoImplementada('Feed URL is not approved')
        return collect_news(url, desde, limite, timeout, until=until, audit=audit)
    if codigo == 'ibge':
        return coletar_ibge(session, url, desde, limite, until=until, audit=audit,
                            non_news_window=non_news_window, news_only=news_only)
    coletor = COLETORES.get(codigo)
    if coletor is None:
        raise FonteNaoImplementada(codigo)
    return coletor(session, url, desde, limite, until=until, audit=audit)
