"""Source collectors for the interfaces that were verified as public and
compatible with the Publications model."""

from __future__ import annotations

from typing import Any

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from .classify import fold

DOMINIO_OFICIAL = {
    'agencia_brasil': {'agenciabrasil.ebc.com.br'},
    'camara': {'dadosabertos.camara.leg.br'},
    'senado': {'legis.senado.leg.br'},
    'ibge': {'servicodados.ibge.gov.br'},
}

MAX_XML_BYTES = 20 * 1024 * 1024
MAX_CAMARA_INVALID_LOGS = 5

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


def _url_oficial(codigo: str, url: object) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError('Source URL is not configured')
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError('Invalid source URL') from exc
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or host not in DOMINIO_OFICIAL[codigo]:
        raise ValueError('Source URL does not belong to the official domain')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Invalid source URL')
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
    raise RespostaInvalida(f'Unsupported publication date: {value!r}')


def _get(session: requests.Session, url: str, params: dict[str, Any], timeout) -> requests.Response:
    try:
        resposta = session.get(url, params=params, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise RuntimeError(f'Source request failed: {exc}') from exc
    if resposta.status_code != 200:
        raise RespostaInvalida(f'Source HTTP {resposta.status_code}')
    return resposta


def _json(response: requests.Response):
    try:
        return response.json()
    except ValueError as exc:
        raise RespostaInvalida('Invalid JSON response') from exc


# ---------------------------------------------------------------- Agência Brasil
def coletar_agencia_brasil(session, url, desde, limite):
    url = _url_oficial('agencia_brasil', url)
    resposta = _get(session, url, {}, (10, 40))
    conteudo = resposta.content
    # The stdlib XML parser expands internal entities.  Refuse documents that
    # carry a DTD (the only way to declare entities) and cap the payload, which
    # removes both the external-entity (XXE) and entity-expansion vectors.
    if len(conteudo) > MAX_XML_BYTES:
        raise RespostaInvalida('RSS document exceeds the size limit')
    if b'<!DOCTYPE' in conteudo.upper() or b'<!ENTITY' in conteudo.upper():
        raise RespostaInvalida('RSS document declares a DTD')
    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError as exc:
        raise RespostaInvalida('Invalid RSS document') from exc
    itens = []
    for elemento in raiz.iter('item'):
        def campo(nome, *, elemento=elemento):
            alvo = elemento.find(nome)
            return alvo.text if alvo is not None else None

        link = _texto(campo('link'), 2000)
        titulo = _texto(campo('title'))
        if not link or not titulo:
            raise RespostaInvalida('RSS item without title or link')
        try:
            data = _data_hora(campo('pubDate'))
        except RespostaInvalida:
            data = None
        if data and data < desde.strftime('%Y-%m-%d %H:%M:%S'):
            continue
        itens.append(Item(
            identificador=link,
            titulo=titulo,
            url=link,
            data_publicacao=data,
            resumo=_html_para_texto(campo('description')),
            tipo=TIPOS['agencia_brasil'],
        ))
        if len(itens) >= limite:
            break
    return itens


# ----------------------------------------------------------------------- Câmara
def coletar_camara(session, url, desde, limite):
    url = _url_oficial('camara', url)
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
            'dataFim': datetime.now(UTC).astimezone().strftime('%Y-%m-%d'),
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
            if data and data < desde.strftime('%Y-%m-%d %H:%M:%S'):
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
def coletar_senado(session, url, desde, limite):
    url = _url_oficial('senado', url)
    dados = _json(_get(session, url, {
        'dataInicioApresentacao': desde.strftime('%Y-%m-%d'),
        'dataFimApresentacao': datetime.now(UTC).astimezone().strftime('%Y-%m-%d'),
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
        if data and data < desde.strftime('%Y-%m-%d %H:%M:%S'):
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
def coletar_ibge(session, url, desde, limite):
    url = _url_oficial('ibge', url)
    pagina = 1
    itens = []
    identificadores_vistos = set()
    # Keep a constant page size for the whole pagination.
    tamanho_pagina = min(100, limite)
    while len(itens) < limite:
        dados = _json(_get(session, url, {
            # The official API uses `qtd` for the page size; `quantidade` is
            # not recognised and silently falls back to the default.
            'qtd': tamanho_pagina,
            'page': pagina,
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
            if data and data < desde.strftime('%Y-%m-%d %H:%M:%S'):
                continue
            tipo = TIPOS['ibge'] if fold(registro.get('tipo')) == 'noticia' else None
            itens.append(Item(
                identificador=str(identificador),
                titulo=titulo,
                url=link,
                data_publicacao=data,
                resumo=_texto(registro.get('introducao')),
                tipo=tipo,
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


def coletar(codigo: str, session, url, desde, limite) -> list[Item]:
    coletor = COLETORES.get(codigo)
    if coletor is None:
        raise FonteNaoImplementada(codigo)
    return coletor(session, url, desde, limite)
