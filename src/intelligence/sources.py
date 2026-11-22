"""Source collectors for the interfaces that were verified as public and
compatible with the Publications model."""

from __future__ import annotations

from typing import Any

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import requests

from .classify import fold
from .network import read_public, validate_url

DOMINIO_OFICIAL = {
    'agencia_brasil': {'agenciabrasil.ebc.com.br'},
    'camara': {'dadosabertos.camara.leg.br'},
    'senado': {'legis.senado.leg.br'},
    'ibge': {'servicodados.ibge.gov.br'},
}

MAX_XML_BYTES = 20 * 1024 * 1024
MAX_CAMARA_INVALID_LOGS = 5

EBC_HOST = 'agenciabrasil.ebc.com.br'
EBC_IMAGE_HOST = 'imagens.ebc.com.br'
IBGE_ARTICLE_HOST = 'agenciadenoticias.ibge.gov.br'
EBC_FEEDS = {
    f'https://{EBC_HOST}/rss/ultimasnoticias/feed.xml',
    f'https://{EBC_HOST}/feed/ultimasnoticias/feed.xml',
}
RSS_PUBLISHERS = {
    **dict.fromkeys(EBC_FEEDS, EBC_HOST),
    'https://www.camara.leg.br/noticias/rss/dinamico/EDUCACAO': 'www.camara.leg.br',
    'https://www.camara.leg.br/noticias/rss/dinamico/SAUDE': 'www.camara.leg.br',
    'https://www.camara.leg.br/noticias/rss/dinamico/TRABALHO': 'www.camara.leg.br',
    'https://agencia.fiocruz.br/rss.xml': 'agencia.fiocruz.br',
    'https://revistapesquisa.fapesp.br/feed/': 'revistapesquisa.fapesp.br',
    'https://www.gov.br/saude/pt-br/assuntos/noticias/noticias/atom.xml': 'www.gov.br',
    'https://portal.tcu.gov.br/rss.xml': 'portal.tcu.gov.br',
}
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
        return candidato.astimezone().astimezone(
            __import__('datetime').timezone.utc
        ).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')
    raise RespostaInvalida('Unsupported publication date')


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime('%Y-%m-%d %H:%M:%S')


def _date_in_window(value: str | None, start: str, end: str | None) -> bool:
    if value is None:
        return end is None
    return value >= start and (end is None or value < end)


def _get(session: requests.Session, url: str, params: dict[str, Any], timeout) -> requests.Response:
    try:
        resposta = session.get(url, params=params, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise RuntimeError('Source request failed') from exc
    if resposta.status_code != 200:
        raise RespostaInvalida(f'Source HTTP {resposta.status_code}')
    return resposta


def _json(response: requests.Response):
    try:
        return response.json()
    except ValueError as exc:
        raise RespostaInvalida('Invalid JSON response') from exc


# ------------------------------------------------------------------------- Feeds
def rss_publisher(url: object) -> str | None:
    return RSS_PUBLISHERS.get(url) if isinstance(url, str) else None


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
) -> list[Item]:
    host = rss_publisher(url)
    if host is None:
        raise ValueError('Feed URL is not approved')
    try:
        endpoints = EBC_FEEDS if host == EBC_HOST else {url}
        body, final_url = read_public(url, allowed_urls=endpoints, max_bytes=MAX_XML_BYTES, timeout=timeout)
    except ValueError as exc:
        raise RespostaInvalida('Feed request destination is not approved') from exc
    except RuntimeError as exc:
        raise RespostaInvalida(str(exc)) from exc
    # Decode before checking for DTDs so alternate encodings cannot hide declarations.
    try:
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
    channel = root.find('channel') if root.tag == 'rss' and root.get('version') == '2.0' else None
    if atom:
        entries = root.findall(ATOM + 'entry')
    elif channel is not None:
        entries = channel.findall('item')
    else:
        raise RespostaInvalida('Unexpected feed format')

    items = []
    identifiers: set[str] = set()
    links: set[str] = set()
    start = _utc_timestamp(since)
    end = _utc_timestamp(until) if until is not None else None
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
            date = entry.findtext('pubDate') or entry.findtext(DC_DATE)
            summary = _field(entry, 'description')
            content = _field(entry, CONTENT)
        link = _linked_url(final_url, address, host)
        if not title or not link:
            raise RespostaInvalida('Feed item has no title or approved link')
        identifier = link if host == EBC_HOST else identity or link
        try:
            published = _data_hora(date)
        except RespostaInvalida:
            published = None
        if not _date_in_window(published, start, end) or identifier in identifiers or link in links:
            continue
        identifiers.add(identifier)
        links.add(link)
        items.append(Item(
            identificador=identifier, titulo=title, url=link, data_publicacao=published,
            resumo=_html_para_texto(summary if host == EBC_HOST else summary or content), tipo='Notícia',
            image_url=_feed_image(entry, link, host, summary, content),
        ))
        if len(items) >= limit:
            break
    return items


def coletar_agencia_brasil(session, url, desde, limite, *, until: datetime | None = None):
    if rss_publisher(url) != EBC_HOST:
        raise ValueError('Feed URL is not approved for this source')
    return collect_feed(url, desde, limite, (10, 40), until=until)


# ----------------------------------------------------------------------- Câmara
def coletar_camara(session, url, desde, limite, *, until: datetime | None = None):
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
        }, (10, 40)))
        if not isinstance(dados, dict) or not isinstance(dados.get('dados'), list):
            raise RespostaInvalida('Invalid proposition response')
        lote = dados['dados']
        if not lote:
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
                    continue
                identificadores_vistos.add(identificador)
                ementa = _texto(registro.get('ementa'))
                uri = _texto(registro.get('uri'), 2000)
                if not ementa:
                    raise RespostaInvalida('Missing proposition summary')
                if not uri:
                    raise RespostaInvalida('Missing proposition URL')
                try:
                    data = _data_hora(registro.get('dataApresentacao'))
                except RespostaInvalida as exc:
                    raise RespostaInvalida('Invalid proposition date') from exc
                numero = registro.get('numero')
                if type(numero) is not int or numero <= 0:
                    raise RespostaInvalida('Invalid proposition number')
            except RespostaInvalida as exc:
                invalidos += 1
                if primeiro_invalido is None:
                    primeiro_invalido = str(exc)
                if invalidos <= MAX_CAMARA_INVALID_LOGS:
                    logger.warning(
                        'Source camara: skipped invalid proposition page=%d id=%s reason=%s',
                        pagina, identificador if type(identificador) is int else 'unknown', exc,
                    )
                continue

            identificadores_aceitos.add(identificador)
            validos += 1
            if not _date_in_window(data, start, end):
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
            if len(itens) >= limite:
                break
        if len(identificadores_vistos) == antes or len(lote) < tamanho_pagina:
            break
        pagina += 1
        if pagina > 50:
            raise RespostaInvalida('Proposition pagination limit exceeded')
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
def coletar_senado(session, url, desde, limite, *, until: datetime | None = None):
    url = _url_oficial('senado', url)
    start = _utc_timestamp(desde)
    end = _utc_timestamp(until) if until is not None else None
    dados = _json(_get(session, url, {
        'dataInicioApresentacao': desde.strftime('%Y-%m-%d'),
        'dataFimApresentacao': (desde if until is not None else datetime.now(UTC).astimezone()).strftime('%Y-%m-%d'),
    }, (10, 60)))
    if not isinstance(dados, list):
        raise RespostaInvalida('Invalid process response')
    itens = []
    for registro in dados:
        if not isinstance(registro, dict):
            raise RespostaInvalida('Invalid process record')
        identificador = registro.get('codigoMateria')
        ementa = _texto(registro.get('ementa'))
        if type(identificador) is not int or identificador <= 0 or not ementa:
            raise RespostaInvalida('Invalid process record')
        try:
            data = _data_hora(registro.get('dataApresentacao'))
        except RespostaInvalida as exc:
            raise RespostaInvalida('Invalid process date') from exc
        if not _date_in_window(data, start, end):
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
        if len(itens) >= limite:
            break
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


def coletar_ibge(session, url, desde, limite, *, until: datetime | None = None):
    url = _url_oficial('ibge', url)
    start = _utc_timestamp(desde)
    end = _utc_timestamp(until) if until is not None else None
    pagina = 1
    itens = []
    identificadores_vistos = set()
    # Keep a constant page size for the whole pagination.
    tamanho_pagina = min(100, limite)
    date_params = {}
    if until is not None:
        date = desde.strftime('%m/%d/%Y')
        date_params = {'de': date, 'ate': date}
    while len(itens) < limite:
        dados = _json(_get(session, url, {
            # The official API uses `qtd` for the page size; `quantidade` is
            # not recognised and silently falls back to the default.
            'qtd': tamanho_pagina,
            'page': pagina,
            **date_params,
        }, (10, 40)))
        if not isinstance(dados, dict) or not isinstance(dados.get('items'), list):
            raise RespostaInvalida('Invalid news response')
        lote = dados['items']
        if not lote:
            break
        antes = len(identificadores_vistos)
        for registro in lote:
            identificador = registro.get('id')
            titulo = _texto(registro.get('titulo'))
            link = _texto(registro.get('link'), 2000)
            if type(identificador) is not int or identificador <= 0 or not titulo or not link:
                raise RespostaInvalida('Invalid news record')
            if identificador in identificadores_vistos:
                continue
            identificadores_vistos.add(identificador)
            try:
                data = _data_hora(registro.get('data_publicacao'))
            except RespostaInvalida as exc:
                raise RespostaInvalida('Invalid news date') from exc
            if not _date_in_window(data, start, end):
                continue
            tipo = TIPOS['ibge'] if fold(registro.get('tipo')) == 'noticia' else None
            itens.append(Item(
                identificador=str(identificador),
                titulo=titulo,
                url=link,
                data_publicacao=data,
                resumo=_texto(registro.get('introducao')),
                tipo=tipo,
                image_url=ibge_image_url(registro.get('imagens')),
            ))
            if len(itens) >= limite:
                break
        if len(identificadores_vistos) == antes or len(lote) < tamanho_pagina:
            break
        pagina += 1
        if pagina > 50:
            raise RespostaInvalida('News pagination limit exceeded')
    return itens


COLETORES = {
    'agencia_brasil': coletar_agencia_brasil,
    'camara': coletar_camara,
    'senado': coletar_senado,
    'ibge': coletar_ibge,
}


def coletar(
    codigo: str, session, url, desde, limite, *, method=None, timeout=(10, 40), until: datetime | None = None,
) -> list[Item]:
    if codigo == 'agencia_brasil' or (method == 'RSS' and codigo not in COLETORES):
        publisher = rss_publisher(url)
        if publisher is None or (codigo == 'agencia_brasil' and publisher != EBC_HOST):
            raise FonteNaoImplementada('Feed URL is not approved')
        return collect_feed(url, desde, limite, timeout, until=until)
    coletor = COLETORES.get(codigo)
    if coletor is None:
        raise FonteNaoImplementada(codigo)
    return coletor(session, url, desde, limite, until=until)
