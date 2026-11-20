"""Data normalization utilities."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

PNCP_ID = re.compile(r'^\d{14}-\d-\d{6}/\d{4}$')
TZ_BRAZIL = ZoneInfo('America/Sao_Paulo')


def odoo_datetime(value: object) -> str | None:
    if not value:
        return None
    try:
        iso = str(value).replace('Z', '+00:00')
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_BRAZIL)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid datetime value') from exc


def clean_money(value: object) -> float | None:
    if value is None or value == '':
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation()
        return float(amount)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Invalid numeric value') from exc


def clean_day(value: object) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def normalize(record: dict) -> dict:
    identifier = record.get('numeroControlePNCP')
    if not isinstance(identifier, str) or not PNCP_ID.fullmatch(identifier):
        raise ValueError('Invalid or missing record identifier')

    organization = record.get('orgaoEntidade') or {}
    unit = record.get('unidadeOrgao') or {}
    if not isinstance(organization, dict) or not isinstance(unit, dict):
        raise ValueError('Unexpected organization or unit format')

    base = {'pncp_id': identifier}
    direct = {
        'objeto': ('objetoCompra', lambda value: value),
        'numero_edital': ('numeroCompra', lambda value: value),
        'modalidade': ('modalidadeNome', lambda value: value),
        'situacao': ('situacaoCompraNome', lambda value: value),
        'valor_estimado': ('valorTotalEstimado', clean_money),
        'data_publicacao': ('dataPublicacaoPncp', clean_day),
        'data_encerramento': ('dataEncerramentoProposta', odoo_datetime),
        'url_origem': ('linkSistemaOrigem', lambda value: value),
    }
    for target, (source, convert) in direct.items():
        if source in record:
            base[target] = convert(record[source])

    # The first response key is authoritative when it is present,
    # including when its value is explicitly null.
    for source in ('dataAtualizacaoGlobal', 'dataAtualizacao'):
        if source in record:
            base['data_atualizacao'] = clean_day(record[source])
            break

    nested = {
        'orgao': (organization, ('razaoSocial', 'razaosocial')),
        'cnpj': (organization, ('cnpj',)),
        'uf': (unit, ('ufSigla',)),
        'municipio': (unit, ('municipioNome',)),
    }
    for target, (data, sources) in nested.items():
        for source in sources:
            if source in data:
                base[target] = data[source]
                break
        else:
            # Explicitly null parent objects represent explicit clearing.
            parent = ('orgaoEntidade' if target in ('orgao', 'cnpj')
                      else 'unidadeOrgao')
            if parent in record and record[parent] is None:
                base[target] = None

    content = {key: value for key, value in base.items() if value is not None}
    base['assinatura'] = hashlib.sha256(
        json.dumps(
            content,
            sort_keys=True,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode()
    ).hexdigest()
    return base
