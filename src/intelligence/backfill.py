"""Bounded recovery of images for existing publications."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import re
import sys
from time import monotonic
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from .images import image_allowed, image_reference, prepare_image
from .network import read_public
from .odoo import relation_id
from .sources import Item, coletar, ibge_image_url, rss_publisher

_MAX_RECORDS = 500
_MAX_RUNTIME = 300
_IBGE_API = 'https://servicodados.ibge.gov.br/api/v3/noticias'
_IBGE_ARTICLE = re.compile(r'^/agencia-(?:noticias|sala-de-imprensa)/(?:[a-z0-9-]+/)*([1-9][0-9]*)-[^/]+$')
_LEGISLATIVE_SOURCES = {'camara', 'senado'}


def _ibge_article_matches(url: str, identifier: str) -> bool:
    parts = urlsplit(url)
    match = _IBGE_ARTICLE.fullmatch(parts.path)
    return (
        parts.hostname == 'agenciadenoticias.ibge.gov.br'
        and match is not None and match[1] == identifier
    )


def _historic_ibge_reference(row, article_url: str, identifier: str, timeout) -> str | None:
    published = row.get('x_studio_data_de_publicacao')
    if not isinstance(published, str):
        return None
    day = datetime.fromisoformat(published).replace(tzinfo=UTC).astimezone(
        ZoneInfo('America/Sao_Paulo')
    ).strftime('%m/%d/%Y')
    endpoint = _IBGE_API + '?' + urlencode({'qtd': 100, 'de': day, 'ate': day})
    body, _ = read_public(endpoint, allowed_urls={endpoint}, max_bytes=2 * 1024 * 1024, timeout=timeout)
    response = json.loads(body)
    if not isinstance(response, dict) or not isinstance(response.get('items'), list):
        raise RuntimeError('Invalid historical IBGE response')
    items = response['items']
    if len(items) > 100:
        raise RuntimeError('Oversized historical IBGE response')
    https_url = 'https://' + article_url.split('://', 1)[1]
    matches = [
        item for item in items
        if isinstance(item, dict) and type(item.get('id')) is int and str(item['id']) == identifier
    ]
    if len(matches) > 1:
        raise RuntimeError('Ambiguous historical IBGE publication')
    if matches and matches[0].get('link') in (https_url, https_url.replace('https:', 'http:', 1)):
        return ibge_image_url(matches[0].get('imagens'))
    return None


def _feed_images(source, settings, session, timeout) -> dict[str, Item]:
    ago = datetime.now(UTC) - timedelta(days=settings.max_age_days)
    items = coletar(
        source['x_studio_codigo_da_fonte_1'], session, source.get('x_studio_url'), ago,
        settings.max_items_per_source, method=source.get('x_studio_metodo_de_coleta'), timeout=timeout,
    )
    return {item.identificador: item for item in items}


def _locate(settings, session, timeout, feeds, source, row, article, identifier) -> str | None:
    code = source['x_studio_codigo_da_fonte_1']
    permissions = settings.image_permissions
    image = None
    metadata_error = None
    if code == 'ibge':
        if not _ibge_article_matches(article, identifier):
            return None
        try:
            image = _historic_ibge_reference(row, article, identifier, timeout)
        except (ValueError, RuntimeError) as exc:
            metadata_error = exc
    elif source.get('x_studio_metodo_de_coleta') == 'RSS':
        source_id = source['id']
        if (
            rss_publisher(source.get('x_studio_url')) != urlsplit(article).hostname
            or (code == 'agencia_brasil' and identifier != article)
        ):
            return None
        if source_id not in feeds:
            try:
                feeds[source_id] = _feed_images(source, settings, session, timeout)
            except (ValueError, RuntimeError) as exc:
                feeds[source_id] = {}
                print(f'Source {code}: backfill feed lookup failed ({type(exc).__name__})', file=sys.stderr)
        item = feeds[source_id].get(identifier)
        if item is not None:
            if item.url != article:
                return None
            image = item.image_url
    else:
        return None
    if image and image_allowed(article, image, permissions=permissions):
        return image
    fallback = image_reference(session, article, permissions=permissions, timeout=timeout)
    if not fallback and metadata_error:
        raise metadata_error
    return fallback or image


def backfill(settings, odoo, session, *, apply: bool, limit: int, after_id: int) -> dict[str, Any]:
    """Report missing Avatars or enrich a page of eligible active records."""
    if not 1 <= limit <= _MAX_RECORDS or after_id < 0:
        raise ValueError(f'Backfill requires a limit of 1 to {_MAX_RECORDS} and a non-negative cursor')
    field = odoo.avatar_field(write=apply)
    if not field:
        raise ValueError('Avatar field has not been confirmed')
    deadline = monotonic() + _MAX_RUNTIME
    missing = odoo.avatar_missing_count(field)
    rows = odoo.avatar_missing_page(field, after_id=after_id, limit=limit)
    sources = {source['id']: source for source in odoo.fontes(include_archived=True)} if rows else {}
    counts: Counter[str] = Counter(dict.fromkeys(
        ('processed', 'identified', 'eligible', 'updated', 'without_image', 'permission_unverified', 'failed'), 0,
    ))
    by_source: dict[str, Counter[str]] = {}
    feeds: dict[int, dict[str, Item]] = {}
    permissions = settings.image_permissions
    connect, read = settings.request_timeout
    timeout = (min(connect, 5), min(read, 15))
    cursor = after_id
    retry_after_id = None
    for row in rows:
        if monotonic() >= deadline:
            break
        previous_cursor = cursor
        cursor = row['id']
        source_id = relation_id(row.get('x_studio_fontes'))
        source = sources.get(source_id, {}) if source_id is not None else {}
        code = source.get('x_studio_codigo_da_fonte_1', 'unknown')
        scoped = by_source.setdefault(code, Counter())
        scoped['processed'] += 1
        stage = 'metadata'
        try:
            image = None
            article = row.get('x_studio_url')
            identifier = row.get('x_studio_identificador_externo')
            if (
                source_id is not None and source and code not in _LEGISLATIVE_SOURCES
                and isinstance(identifier, str) and isinstance(article, str)
            ):
                if image_allowed(article, permissions=permissions):
                    image = _locate(settings, session, timeout, feeds, source, row, article, identifier)
                else:
                    scoped['permission_unverified'] += 1
                    continue
            if image is None or not isinstance(article, str):
                scoped['without_image'] += 1
                continue
            scoped['identified'] += 1
            if not image_allowed(article, image, permissions=permissions):
                scoped['permission_unverified'] += 1
                continue
            if apply:
                stage = 'Odoo eligibility check'
                if not odoo.avatar_missing(row['id'], field):
                    continue
            scoped['eligible'] += 1
            if apply:
                stage = 'download or processing'
                encoded = prepare_image(session, image, article_url=article, permissions=permissions, timeout=timeout)
                stage = 'Odoo write or verification'
                if odoo.write_avatar(row['id'], field, encoded, expected=row):
                    scoped['updated'] += 1
        except (ValueError, RuntimeError) as exc:
            if retry_after_id is None:
                retry_after_id = previous_cursor
            scoped['failed'] += 1
            if scoped['failed'] <= 3:
                print(
                    f'Image backfill failed during {stage} ({type(exc).__name__})',
                    file=sys.stderr,
                )
    for scoped in by_source.values():
        counts.update(scoped)
    return {
        **counts,
        'missing': missing,
        'next_after_id': cursor if counts['processed'] < len(rows) or len(rows) == limit else None,
        'retry_after_id': retry_after_id,
        'sources': {code: dict(scoped) for code, scoped in by_source.items()},
    }
