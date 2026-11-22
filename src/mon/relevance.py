"""Deterministic filtering utilities."""

from __future__ import annotations

from typing import Any

import re
import unicodedata

from .config import Filters

NR_SEPARATOR = r'\s*(?:[-–—]\s*)?'
NR_TERM = re.compile(rf'nr{NR_SEPARATOR}0*([1-9][0-9]*)')


def fold(s: object) -> str:
    text = unicodedata.normalize(
        'NFKD',
        str(s or '').casefold(),
    )
    return ''.join(
        c for c in text
        if not unicodedata.combining(c)
    )


def _contains_term(text: str, term: str) -> bool:
    term = fold(term)
    if term == 'aep':
        return re.search(r'\baep\b', text) is not None
    norm = NR_TERM.fullmatch(term)
    if norm:
        return re.search(rf'\bnr{NR_SEPARATOR}0*{norm[1]}\b', text) is not None
    return term in text


def is_relevant(record: dict[str, Any], filt: Filters) -> bool:
    unit = record.get('unidadeOrgao')
    if unit is not None and not isinstance(unit, dict):
        raise ValueError('Unexpected organization unit format')
    uf = str((unit or {}).get('ufSigla') or '').upper()

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
        _contains_term(text, term)
        for term in filt.exclude_any
    ):
        return False

    return any(
        _contains_term(text, term)
        for term in filt.include_any
    )
