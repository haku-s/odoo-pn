"""Normalize verified consultation response fields."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .config import BRASILIA

PNCP_ID = re.compile(r'([0-9]{14})-1-([0-9]{6})/([0-9]{4})')
PROPOSAL_TIME = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}'
    r'(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?'
)


def odoo_datetime(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not PROPOSAL_TIME.fullmatch(value):
        raise ValueError('Invalid proposal deadline')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BRASILIA)
        return dt.astimezone(timezone.utc).isoformat(sep=' ', timespec='seconds')[:19]
    except (ValueError, OverflowError) as exc:
        raise ValueError('Invalid proposal deadline') from exc


def clean_money(value: object) -> float | None:
    if value is None:
        return None
    try:
        if type(value) not in (str, int, float) or (
            isinstance(value, str) and not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value)
        ):
            raise InvalidOperation()
        amount = float(Decimal(str(value)))
        if not math.isfinite(amount) or amount < 0:
            raise InvalidOperation()
        return amount
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError('Invalid estimated value') from exc


def purchase_identity(identifier: object) -> tuple[str, int, int]:
    match = PNCP_ID.fullmatch(identifier) if isinstance(identifier, str) else None
    if not match or int(match[2]) == 0 or int(match[3]) == 0:
        raise ValueError('Invalid or missing record identifier')
    return match[1], int(match[3]), int(match[2])


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('Unexpected text field value')
    return value.strip() or None


def _object(record: dict, key: str) -> dict:
    value = record.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('Unexpected organization or unit format')
    return value


def normalize(record: dict) -> dict:
    identifier = record.get('numeroControlePNCP')
    cnpj, year, sequence = purchase_identity(identifier)
    org = _object(record, 'orgaoEntidade')
    unit = _object(record, 'unidadeOrgao')
    for source, key, expected in (
        (org, 'cnpj', cnpj),
        (record, 'anoCompra', year),
        (record, 'sequencialCompra', sequence),
    ):
        if key in source and (
            type(source[key]) is not type(expected) or source[key] != expected
        ):
            raise ValueError('Purchase identity does not match its source fields')

    result = {'pncp_id': identifier}
    for target, source in (
        ('title', 'objetoCompra'),
        ('process_number', 'processo'),
        ('purchase_number', 'numeroCompra'),
        ('modality', 'modalidadeNome'),
    ):
        if source in record:
            result[target] = _text(record[source])
    if 'dataEncerramentoProposta' in record:
        result['deadline'] = odoo_datetime(record['dataEncerramentoProposta'])
    if 'valorTotalEstimado' in record:
        result['estimated_value'] = clean_money(record['valorTotalEstimado'])
        secrecy = record.get('orcamentoSigilosoCodigo')
        if secrecy is not None:
            if type(secrecy) is not int or secrecy not in (1, 2, 3):
                raise ValueError('Invalid budget confidentiality code')
            if secrecy != 1:
                result['estimated_value'] = None
        result['estimate_needs_confirmation'] = (
            result['estimated_value'] is not None and secrecy is None
        )

    for key in ('razaoSocial', 'razaosocial'):
        if key in org:
            result['organization'] = _text(org[key])
            break
    if any(key in unit for key in ('municipioNome', 'ufSigla')):
        result['municipality_state'] = ' / '.join(filter(None, (
            _text(unit.get('municipioNome')), _text(unit.get('ufSigla')),
        ))) or None
    return result
