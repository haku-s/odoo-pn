"""Deterministic subject classification."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


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
        term = fold(line).strip()
        if term and term not in terms:
            terms.append(term)
    return tuple(terms)


def _pattern(term: str) -> re.Pattern:
    return re.compile(rf'\b{re.escape(term)}\b')


@dataclass(frozen=True)
class Classificacao:
    assunto_id: int | None
    assunto_nome: str | None
    pontuacao: int
    ambigua: bool
    sem_correspondencia: bool
    excluido: bool
    detalhes: dict[str, int] = field(default_factory=dict)


def classificar(titulo: object, resumo: object, assuntos: list[dict]) -> Classificacao:
    texto = fold(' '.join(part for part in (titulo, resumo) if part))
    if not texto.strip():
        return Classificacao(None, None, 0, False, True, False, {})

    pontuacoes: dict[int, int] = {}
    nomes: dict[int, str] = {}
    detalhes: dict[str, int] = {}
    excluidos: set[int] = set()
    for assunto in assuntos:
        identificador = assunto.get('id')
        if type(identificador) is not int:
            raise ValueError('Invalid subject identifier')
        nomes[identificador] = str(assunto.get('x_name') or '')
        exclusoes = split_terms(assunto.get('x_studio_termos_de_exclusao'))
        if any(_pattern(termo).search(texto) for termo in exclusoes):
            # An exclusion applies only to the subject that declares it; the
            # remaining subjects stay eligible.
            excluidos.add(identificador)
            detalhes[nomes[identificador]] = 0
            continue
        inclusoes = split_terms(assunto.get('x_studio_termos_de_inclusao'))
        acertos = sum(
            1 for termo in inclusoes if _pattern(termo).search(texto)
        )
        pontuacoes[identificador] = acertos
        detalhes[nomes[identificador]] = acertos

    candidatos = {i: p for i, p in pontuacoes.items() if p > 0}
    if not candidatos:
        if excluidos:
            return Classificacao(None, None, 0, False, False, True, detalhes)
        return Classificacao(None, None, 0, False, True, False, detalhes)
    melhor = max(candidatos.values())
    vencedores = [i for i, p in candidatos.items() if p == melhor]
    if len(vencedores) > 1:
        return Classificacao(None, None, melhor, True, False, False, detalhes)
    identificador = vencedores[0]
    return Classificacao(
        identificador, nomes[identificador], melhor, False, False, False, detalhes,
    )
