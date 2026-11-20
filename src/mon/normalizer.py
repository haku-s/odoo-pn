
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

        return dt.astimezone(timezone.utc).strftime(
            '%Y-%m-%d %H:%M:%S'
        )

    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid datetime value') from exc


def clean_money(value: object) -> float | None:
    if value is None or value == '':
        return None

    try:
        v = Decimal(str(value))

        if not v.is_finite():
            raise InvalidOperation()

        return float(v)

    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Invalid numeric value') from exc


def clean_day(value: object) -> str | None:
    if not value:
        return None

    return str(value)[:10]


def normalize(r: dict) -> dict:
    pncp_id = r.get('numeroControlePNCP')

    if (
        not isinstance(pncp_id, str)
        or not PNCP_ID.fullmatch(pncp_id)
    ):
        raise ValueError('Invalid or missing record identifier')

    org = r.get('orgaoEntidade') or {}
    unit = r.get('unidadeOrgao') or {}

    base = {
        'pncp_id': pncp_id,
        'objeto': r.get('objetoCompra'),
        'numero_edital': r.get('numeroCompra'),
        'orgao': (
            org.get('razaoSocial')
            or org.get('razaosocial')
        ),
        'cnpj': org.get('cnpj'),
        'uf': unit.get('ufSigla'),
        'municipio': unit.get('municipioNome'),
        'modalidade': r.get('modalidadeNome'),
        'situacao': r.get('situacaoCompraNome'),
        'valor_estimado': clean_money(
            r.get('valorTotalEstimado')
        ),
        'data_publicacao': clean_day(
            r.get('dataPublicacaoPncp')
        ),
        'data_atualizacao': clean_day(
            r.get('dataAtualizacaoGlobal')
            or r.get('dataAtualizacao')
        ),
        'data_encerramento': odoo_datetime(
            r.get('dataEncerramentoProposta')
        ),
        'url_origem': r.get('linkSistemaOrigem'),
    }

    content = {
        k: v
        for k, v in base.items()
        if v is not None
    }

    base['assinatura'] = hashlib.sha256(
        json.dumps(
            content,
            sort_keys=True,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode()
    ).hexdigest()

    return base
