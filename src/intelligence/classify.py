"""Deterministic subject classification."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


def fold(value: object) -> str:
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    return ''.join(ch for ch in text if not unicodedata.combining(ch))


def split_terms(value: object) -> tuple[str, ...]:
    """Terms are stored one expression per line."""
    if value is None or value is False:
        return ()
    if not isinstance(value, str):
        raise ValueError('Invalid term list')
    terms = []
    for line in value.splitlines():
        term = _normalise(line)
        if term and term not in terms:
            terms.append(term)
    return tuple(terms)


def _normalise(value: object) -> str:
    return re.sub(r'[\s\-\u2010-\u2015]+', ' ', fold(value)).strip()


@lru_cache(maxsize=512)
def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf'(?<!\w){re.escape(term)}(?!\w)')


@lru_cache(maxsize=512)
def _compound_key(term: str) -> str:
    words = term.split()
    if len(words) != 2 or any(len(word) < 5 or not word.isalpha() for word in words):
        return term
    singular = []
    for word in words:
        if word.endswith(('ais', 'eis', 'ois', 'uis')):
            singular.append(word[:-2] + 'l')
        elif word.endswith(('as', 'es', 'os')):
            singular.append(word[:-1])
        else:
            return term
    return ' '.join(singular)


@dataclass(frozen=True)
class Classificacao:
    assunto_id: int | None
    assunto_nome: str | None
    pontuacao: int
    ambigua: bool
    sem_correspondencia: bool
    excluido: bool
    detalhes: dict[str, int] = field(default_factory=dict)
    termos_correspondentes: tuple[str, ...] = ()
    inclusion_matches: dict[int, tuple[str, ...]] = field(default_factory=dict)
    exclusion_matches: dict[int, tuple[str, ...]] = field(default_factory=dict)


def classificar(titulo: object, resumo: object, assuntos: list[dict[str, Any]]) -> Classificacao:
    texts = (_normalise(titulo), _normalise(resumo))
    if not any(texts):
        return Classificacao(None, None, 0, False, True, False, {})

    names: dict[int, str] = {}
    spans: dict[int, dict[str, tuple[tuple[int, int, int], ...]]] = {}
    exclusions: dict[int, tuple[str, ...]] = {}
    for assunto in assuntos:
        identificador = assunto.get('id')
        if type(identificador) is not int:
            raise ValueError('Invalid subject identifier')
        names[identificador] = str(assunto.get('x_name') or '')
        matches = {}
        for term in split_terms(assunto.get('x_studio_termos_de_inclusao')):
            forms = dict.fromkeys((term, _compound_key(term)))
            found = tuple((index, *match.span()) for index, text in enumerate(texts)
                          for form in forms for match in _pattern(form).finditer(text))
            if found:
                matches[term] = found
        spans[identificador] = matches
        if matches:
            blocked = tuple(term for term in split_terms(assunto.get('x_studio_termos_de_exclusao'))
                            if any(_pattern(term).search(text) for text in texts))
            if blocked:
                exclusions[identificador] = blocked

    # A nested match is the same evidence as its longer expression.
    eligible = {key: matches for key, matches in spans.items() if key not in exclusions}
    by_field: dict[int, set[tuple[int, int]]] = {0: set(), 1: set()}
    for matches in eligible.values():
        for found in matches.values():
            for index, begin, end in found:
                by_field[index].add((begin, end))
    independent = set()
    for index, intervals in by_field.items():
        rightmost = -1
        for begin, end in sorted(intervals, key=lambda pair: (pair[0], -pair[1])):
            if end > rightmost:
                independent.add((index, begin, end))
                rightmost = end
    terms = {
        key: tuple(term for term, found in matches.items() if any(span in independent for span in found))
        for key, matches in eligible.items()
    }
    scores = {key: len({_compound_key(term) for term in terms.get(key, ())}) for key in names}
    details = {name: scores[key] for key, name in names.items()}
    evidence = {key: tuple(matches) for key, matches in spans.items() if matches}
    best = max(scores.values(), default=0)
    if not best:
        return Classificacao(None, None, 0, False, not exclusions, bool(exclusions), details,
                             inclusion_matches=evidence, exclusion_matches=exclusions)
    winners = [key for key, score in scores.items() if score == best]
    if len(winners) > 1:
        return Classificacao(None, None, best, True, False, False, details,
                             inclusion_matches=evidence, exclusion_matches=exclusions)
    winner = winners[0]
    return Classificacao(winner, names[winner], best, False, False, False, details,
                         terms[winner], evidence, exclusions)
