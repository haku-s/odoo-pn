"""Runtime configuration for the data sync package."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def _positive_int(var: str, default: int, minimum: int = 1, maximum: int = 100000) -> int:
    raw = os.getenv(var)
    if raw is None or raw == '':
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f'{var} must be an integer') from exc
    if not minimum <= value <= maximum:
        raise ValueError(f'{var} must be between {minimum} and {maximum}')
    return value


def _timeout(var: str, default: int) -> tuple[int, int]:
    raw = os.getenv(var)
    if raw is None or raw == '':
        return (10, default)
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValueError(f'{var} must be an integer') from exc
    if seconds <= 0:
        raise ValueError(f'{var} must be positive')
    return (10, seconds)


def _image_permissions() -> dict[str, str]:
    raw = os.getenv('INTEL_IMAGE_PERMISSIONS', '')
    if not raw:
        return {}
    if len(raw) > 128_000:
        raise ValueError('INTEL_IMAGE_PERMISSIONS exceeds its size limit')
    try:
        values = json.loads(raw)
    except ValueError as exc:
        raise ValueError('Invalid INTEL_IMAGE_PERMISSIONS JSON') from exc
    if not isinstance(values, dict) or any(
        not isinstance(value, str) or not key.startswith('https://') or not value.startswith('https://')
        for key, value in values.items()
    ):
        raise ValueError('INTEL_IMAGE_PERMISSIONS must map HTTPS article URLs to HTTPS image URLs')
    return values


@dataclass(frozen=True)
class Settings:
    odoo_url: str
    odoo_database: str
    odoo_api_key: str
    max_items_per_source: int
    max_age_days: int
    request_timeout: tuple[int, int]
    avatar_field: str | None = None
    image_permissions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> 'Settings':
        url = os.getenv('ODOO_URL', '').rstrip('/')
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise ValueError('Invalid ODOO_URL configuration') from exc
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ('', '/')
        ):
            # The JSON-2 API is served from the base origin; an application
            # path such as `/odoo` changes the routing behaviour.
            raise ValueError('Invalid ODOO_URL configuration')
        return cls(
            odoo_url=url,
            odoo_database=os.getenv('ODOO_DATABASE', ''),
            odoo_api_key=os.getenv('ODOO_API_KEY', ''),
            max_items_per_source=_positive_int('INTEL_MAX_ITEMS_PER_SOURCE', 500),
            max_age_days=_positive_int('INTEL_MAX_AGE_DAYS', 30, 1, 366),
            request_timeout=_timeout('INTEL_READ_TIMEOUT', 40),
            avatar_field=os.getenv('INTEL_AVATAR_FIELD') or None,
            image_permissions=_image_permissions(),
        )

    def require_odoo(self, write: bool = False) -> None:
        if not self.odoo_url:
            raise ValueError('Missing ODOO_URL')
        if not self.odoo_api_key:
            raise ValueError('Missing ODOO_API_KEY')
