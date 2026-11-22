"""Verify image permission and prepare a small raster Avatar."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from html.parser import HTMLParser
from io import BytesIO
import re
from urllib.parse import urljoin, urlsplit

from PIL import Image, ImageOps

from .network import read_public, validate_url
from .sources import EBC_HOST, is_ebc_image_proxy

# Publisher article hosts and the paths their feeds use for media.
MEDIA_PATHS: dict[str, tuple[str, ...]] = {
    'agenciabrasil.ebc.com.br': ('/sites/default/files/',),
    'agenciadenoticias.ibge.gov.br': ('/images/agenciadenoticias/',),
    'www.camara.leg.br': ('/midias/image/',),
    'agencia.fiocruz.br': ('/sites/agencia.fiocruz.br/files/',),
    'revistapesquisa.fapesp.br': ('/wp-content/uploads/',),
    'portal.tcu.gov.br': ('/uploads/noticias/capas/',),
}
ARTICLE_PATHS: dict[str, str] = {
    'agenciabrasil.ebc.com.br': r'/[^/]+/noticia/\d{4}-\d{2}/[^/]+/?',
    'agenciadenoticias.ibge.gov.br': r'/agencia-noticias/[^?#]+',
    'www.camara.leg.br': r'/noticias/\d+[^/]*/*',
    'agencia.fiocruz.br': r'/[^/.]+/?',
    'revistapesquisa.fapesp.br': r'/[^/.]+/?',
    'portal.tcu.gov.br': r'/imprensa/noticias/[^/.]+/?',
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_SIDE = 8192
THUMBNAIL = (512, 512)
FORMATS = ('JPEG', 'PNG', 'WEBP')


def _article_https(url: str) -> str:
    if url.startswith('http://agenciadenoticias.ibge.gov.br/'):
        return 'https://' + url.removeprefix('http://')
    return url


def image_allowed(
    article_url: str, image_url: str | None = None, *, permissions: Mapping[str, str],
) -> bool:
    """Require independent permission for the exact article and image."""
    try:
        article_url = _article_https(article_url)
        article = urlsplit(article_url)
        host = article.hostname or ''
        validate_url(article_url, {host})
        if (
            host not in ARTICLE_PATHS or '/..' in article.path or '%' in article.path
            or not re.fullmatch(ARTICLE_PATHS[host], article.path)
        ):
            return False
        permitted = permissions.get(article_url)
        if not permitted or (image_url is not None and image_url != permitted):
            return False
        if host == EBC_HOST and is_ebc_image_proxy(permitted):
            return True
        validate_url(permitted, {host})
        image_path = urlsplit(permitted).path
        # These paths are literal publisher media routes.
        return '%' not in image_path and '..' not in image_path and image_path.startswith(MEDIA_PATHS[host])
    except (TypeError, ValueError):
        return False


class _OpenGraph(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'meta':
            values = dict(attrs)
            value = values.get('content')
            if values.get('property') in ('og:image', 'og:image:secure_url') and value:
                self.images.add(value)


def image_reference(
    _session, article_url: str, *, permissions: Mapping[str, str], timeout: tuple[int, int],
) -> str | None:
    """Read article metadata only when a specific image has reuse permission."""
    article_url = _article_https(article_url)
    if not image_allowed(article_url, permissions=permissions):
        return None
    body, final = read_public(article_url, allowed_urls={article_url}, max_bytes=MAX_PAGE_BYTES, timeout=timeout)
    parser = _OpenGraph()
    parser.feed(body.decode('utf-8', errors='replace'))
    expected = permissions[article_url]
    return expected if any(urljoin(final, value) == expected for value in parser.images) else None


def prepare_image(
    _session, image_url: str, *, article_url: str, permissions: Mapping[str, str],
    timeout: tuple[int, int],
) -> str:
    if not image_allowed(article_url, image_url, permissions=permissions):
        raise ValueError('Image reuse permission or origin was not verified')
    data, _ = read_public(image_url, allowed_urls={image_url}, max_bytes=MAX_IMAGE_BYTES, timeout=timeout)
    try:
        with Image.open(BytesIO(data), formats=FORMATS) as candidate:
            width, height = candidate.size
            if (
                not 0 < width <= MAX_SIDE or not 0 < height <= MAX_SIDE
                or width * height > MAX_PIXELS or getattr(candidate, 'is_animated', False)
            ):
                raise ValueError('Image dimensions or frame count exceed limits')
            candidate.verify()
        with Image.open(BytesIO(data), formats=FORMATS) as candidate:
            transposed = ImageOps.exif_transpose(candidate)
            mode = 'RGBA' if 'A' in transposed.getbands() or 'transparency' in transposed.info else 'RGB'
            converted = transposed.convert(mode)
            converted.thumbnail(THUMBNAIL, Image.Resampling.LANCZOS)
            clean = Image.new(mode, converted.size)
            clean.paste(converted)
            target = BytesIO()
            clean.save(target, 'PNG' if mode == 'RGBA' else 'JPEG', quality=85)
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('Invalid or unsupported raster image') from exc
    return base64.b64encode(target.getvalue()).decode('ascii')
