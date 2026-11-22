"""Prepare attributed raster thumbnails for the existing Odoo Avatar."""

from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .camara_media import credited_photo, eligible_photo, is_article, photo_credit
from .network import read_public

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_SIDE = 8192
FORMATS = ('JPEG', 'PNG', 'WEBP')
WIDTH = 480
HEIGHT = 270
PHOTO_HEIGHT = 220


def image_reference(article_url: str, *, timeout: tuple[int, int]) -> tuple[str, str] | None:
    """Find the credited image on a known Chamber article."""
    if not is_article(article_url):
        return None
    body, final = read_public(article_url, allowed_urls={article_url}, max_bytes=MAX_PAGE_BYTES, timeout=timeout)
    return credited_photo(final, body.decode('utf-8', errors='replace'))


def prepare_image(
    image_url: str, *, article_url: str, credit: str, timeout: tuple[int, int],
) -> str:
    credit_text = photo_credit(credit)
    if not credit_text or not eligible_photo(article_url, image_url, credit_text):
        raise ValueError('Institutional photo or reuse credit could not be verified')
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
            photo = ImageOps.exif_transpose(candidate).convert('RGBA')
            photo.thumbnail((WIDTH, PHOTO_HEIGHT), Image.Resampling.LANCZOS)
            thumbnail = Image.new('RGB', (WIDTH, HEIGHT), '#202124')
            thumbnail.paste(photo, ((WIDTH - photo.width) // 2, (PHOTO_HEIGHT - photo.height) // 2), photo.getchannel('A'))
            draw = ImageDraw.Draw(thumbnail)
            author, publisher = credit_text.split(' / ')
            font = ImageFont.load_default(size=16)
            if draw.textlength(author + ' /', font=font) > WIDTH - 24:
                font = ImageFont.load_default(size=13)
            if draw.textlength(author + ' /', font=font) > WIDTH - 24:
                raise ValueError('Photo credit is too long for the thumbnail')
            draw.text((12, 223), author + ' /', fill='white', font=font)
            draw.text((12, 245), publisher, fill='white', font=font)
            output = BytesIO()
            thumbnail.save(output, 'JPEG', quality=85)
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('Invalid or unsupported raster image') from exc
    return base64.b64encode(output.getvalue()).decode('ascii')
