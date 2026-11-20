"""Runtime configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

BRASILIA = ZoneInfo('America/Sao_Paulo')
CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config' / 'filters.json'
FIELD_ALIASES = {
    'numero_processo': 'process_number',
    'numero_edital': 'purchase_number',
    'modalidade': 'modality',
    'valor_estimado': 'estimated_value',
    'data_encerramento': 'deadline',
    'orgao': 'organization',
    'municipio': 'municipality_state',
}
OFFICIAL_FIELDS = frozenset(FIELD_ALIASES.values())
ALLOWED_FIELDS = OFFICIAL_FIELDS | frozenset(FIELD_ALIASES) | frozenset({
    'objeto', 'cnpj', 'uf',
    'situacao', 'data_publicacao',
    'data_atualizacao', 'url_origem', 'assinatura',
})
TECHNICAL_FIELD = re.compile(r'^[a-z][a-z0-9_]*$')


def _integer(var: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f'{var} must be an integer') from exc


def _positive_int(
    var: str, default: int, minimum: int = 1, maximum: int = 10000,
) -> int:
    value = _integer(var, os.getenv(var, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f'{var} must be between {minimum} and {maximum}')
    return value


@dataclass(frozen=True)
class Filters:
    modalities: tuple[int, ...]
    include_any: tuple[str, ...]
    exclude_any: tuple[str, ...]
    ufs: tuple[str, ...]

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> 'Filters':
        secret = os.getenv('PNCP_FILTERS_JSON', '')
        try:
            obj = json.loads(secret or path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as exc:
            raise ValueError('Invalid PNCP filter JSON') from exc
        if not isinstance(obj, dict):
            raise ValueError('Expected a JSON object')
        modalities = obj.get('modalidades', [])
        include = obj.get('include_any', [])
        exclude = obj.get('exclude_any', [])
        ufs = obj.get('ufs', [])
        if (
            not isinstance(modalities, list)
            or not modalities
            or any(type(item) is not int or item <= 0 for item in modalities)
        ):
            raise ValueError('Invalid modality configuration')
        if any(
            not isinstance(items, list)
            or any(not isinstance(value, str) for value in items)
            for items in (include, exclude, ufs)
        ):
            raise ValueError('Invalid filter configuration')
        if not any(value.strip() for value in include):
            raise ValueError('At least one inclusion term is required')
        codes = [value.strip().upper() for value in ufs]
        if any(not re.fullmatch(r'[A-Z]{2}', value) for value in codes):
            raise ValueError('Invalid region code')
        return cls(
            tuple(dict.fromkeys(modalities)),
            tuple(value.strip() for value in include if value.strip()),
            tuple(value.strip() for value in exclude if value.strip()),
            tuple(dict.fromkeys(codes)),
        )


@dataclass(frozen=True)
class Settings:
    today: date
    initial_date: date
    overlap_days: int
    max_days: int
    page_size: int
    max_pages: int
    odoo_url: str
    odoo_database: str
    odoo_api_key: str
    project_id: int | None
    pncp_field: str
    checkpoint_field: str
    stage_id: int | None
    field_map: dict[str, str]

    @classmethod
    def from_env(cls) -> 'Settings':
        from datetime import datetime
        today = datetime.now(BRASILIA).date()
        try:
            first = date.fromisoformat(os.getenv(
                'PNCP_INITIAL_DATE',
                (today - timedelta(days=2)).isoformat(),
            ))
        except ValueError as exc:
            raise ValueError('Invalid PNCP_INITIAL_DATE') from exc
        if first > today:
            raise ValueError('Initial date cannot be in the future')
        try:
            mapping = json.loads(os.getenv('ODOO_FIELD_MAP', '').strip() or '{}')
        except json.JSONDecodeError as exc:
            raise ValueError('Invalid ODOO_FIELD_MAP JSON') from exc
        if not isinstance(mapping, dict) or set(mapping) - ALLOWED_FIELDS:
            raise ValueError('Invalid source field mapping')
        if any(
            not isinstance(value, str)
            or not TECHNICAL_FIELD.fullmatch(value)
            for value in mapping.values()
        ):
            raise ValueError('Invalid target field name')
        if len(mapping) != len(set(mapping.values())):
            raise ValueError('Duplicate target field mapping')
        if any(
            not target.startswith('x_')
            and (FIELD_ALIASES.get(source, source), target) != ('deadline', 'date_deadline')
            for source, target in mapping.items()
        ):
            raise ValueError('Only custom target fields are supported')

        project = os.getenv('ODOO_PROJECT_ID', '')
        stage = os.getenv('ODOO_STAGE_ID', '')
        pncp_field = os.getenv('ODOO_PNCP_FIELD', '')
        checkpoint_field = os.getenv('ODOO_CHECKPOINT_FIELD', '')
        for name in (pncp_field, checkpoint_field):
            if name and (
                not TECHNICAL_FIELD.fullmatch(name)
                or not name.startswith('x_')
            ):
                raise ValueError('Only custom technical fields are supported')
        if pncp_field and checkpoint_field and pncp_field == checkpoint_field:
            raise ValueError('Reserved fields must be distinct')
        if pncp_field and pncp_field in mapping.values():
            raise ValueError('Reserved field mapping conflict')
        if checkpoint_field and checkpoint_field in mapping.values():
            raise ValueError('Checkpoint field mapping conflict')
        return cls(
            today=today,
            initial_date=first,
            overlap_days=_positive_int('PNCP_OVERLAP_DAYS', 2, 1, 31),
            max_days=_positive_int('PNCP_MAX_DAYS_PER_RUN', 7, 1, 366),
            page_size=_positive_int('PNCP_PAGE_SIZE', 50, 10, 500),
            max_pages=_positive_int('PNCP_MAX_PAGES', 300, 1, 100000),
            odoo_url=os.getenv('ODOO_URL', '').rstrip('/'),
            odoo_database=os.getenv('ODOO_DATABASE', ''),
            odoo_api_key=os.getenv('ODOO_API_KEY', ''),
            project_id=_integer('ODOO_PROJECT_ID', project) if project else None,
            pncp_field=pncp_field,
            checkpoint_field=checkpoint_field,
            stage_id=_integer('ODOO_STAGE_ID', stage) if stage else None,
            field_map=mapping,
        )

    def require_odoo(self, write: bool = False) -> None:
        if not self.odoo_url:
            raise ValueError('Missing ODOO_URL')
        try:
            parsed = urlsplit(self.odoo_url)
        except ValueError as exc:
            raise ValueError('Invalid ODOO_URL configuration') from exc
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError('Invalid ODOO_URL configuration')
        if not self.odoo_api_key:
            raise ValueError('Missing ODOO_API_KEY')
        if not self.project_id or self.project_id <= 0:
            raise ValueError('Missing or invalid ODOO_PROJECT_ID')
        if write and (not self.stage_id or self.stage_id <= 0):
            raise ValueError('Missing or invalid ODOO_STAGE_ID')
        if write and (not self.pncp_field or not self.checkpoint_field):
            raise ValueError('Required write fields are not configured')
        if write and (
            not self.pncp_field.startswith('x_')
            or not self.checkpoint_field.startswith('x_')
            or self.pncp_field == self.checkpoint_field
            or self.pncp_field in self.field_map.values()
            or self.checkpoint_field in self.field_map.values()
        ):
            raise ValueError('Unsafe write field configuration')
