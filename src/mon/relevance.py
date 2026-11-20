"""Deterministic filtering utilities."""

from __future__ import annotations

import unicodedata

from .config import Filters


def fold(s: object) -> str:
    text = unicodedata.normalize(
        'NFKD',
        str(s or '').casefold(),
    )
    return ''.join(
        c for c in text
        if not unicodedata.combining(c)
    )


def is_relevant(record: dict, filt: Filters) -> bool:
    unit = record.get('unidadeOrgao') or {}
    uf = str(unit.get('ufSigla') or '').upper()

    if filt.ufs and uf not in filt.ufs:
        return False

    text = fold(
        ' '.join(
            str(record.get(k) or '')
            for k in (
                'objetoCompra',
                'informacaoComplementar',
            )
        )
    )

    if any(
        fold(term) in text
        for term in filt.exclude_any
    ):
        return False

    return any(
        fold(term) in text
        for term in filt.include_any
    )
