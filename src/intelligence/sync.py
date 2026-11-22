"""Collect, classify and synchronise publications into Odoo."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import sys
from typing import Any

import requests

from .classify import classificar
from .odoo import OdooClient, SELECTIONS, normalizar_valor
from .sources import COLETORES, coletar


def _agora() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _mudou(antigo: dict[str, Any], valores: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields whose normalised stored value differs."""
    return {
        nome: valor for nome, valor in valores.items()
        if normalizar_valor(nome, antigo.get(nome)) != normalizar_valor(nome, valor)
    }


class Synchronizer:
    def __init__(self, cfg, odoo: OdooClient, session: requests.Session, apply: bool = False):
        self.cfg = cfg
        self.odoo = odoo
        self.session = session
        self.apply = apply
        self.stats = Counter()

    def _valores(self, item, assunto_id: int | None) -> dict[str, Any]:
        valores = {
            'x_name': item.titulo,
            'x_studio_fontes': None,
            'x_studio_identificador_externo': item.identificador,
            'x_studio_url': item.url,
            'x_studio_resumo': item.resumo or None,
            'x_studio_assunto_1': assunto_id or False,
        }
        if item.tipo is not None:
            if item.tipo not in SELECTIONS['x_studio_tipo_de_publicacao']:
                raise ValueError(f'Unsupported publication type: {item.tipo}')
            valores['x_studio_tipo_de_publicacao'] = item.tipo
        if item.data_publicacao is not None:
            valores['x_studio_data_de_publicacao'] = item.data_publicacao
        return valores

    def _processar_fonte(self, fonte: dict[str, Any], assuntos: list[dict[str, Any]]) -> None:
        identificador_fonte = fonte['id']
        codigo = fonte['x_studio_codigo_da_fonte_1']
        if not fonte.get('x_studio_coleta_habilitada'):
            self.stats['fontes_desabilitadas'] += 1
            return
        if codigo not in COLETORES:
            self.stats['fontes_nao_implementadas'] += 1
            print(f'Source {codigo}: no collector implemented', file=sys.stderr)
            return
        desde = datetime.now(timezone.utc) - timedelta(days=self.cfg.max_age_days)
        try:
            itens = coletar(
                codigo, self.session, fonte.get('x_studio_url'), desde,
                self.cfg.max_items_per_source,
            )
        except Exception as exc:  # noqa: BLE001 - report and keep other sources running
            self.stats['fontes_com_erro'] += 1
            mensagem = f'{type(exc).__name__}: {exc}'
            print(f'Source {codigo}: collection failed: {mensagem}', file=sys.stderr)
            if self.apply:
                self._registrar_falha(identificador_fonte, codigo, mensagem)
            return

        self.stats['fontes_coletadas'] += 1
        self.stats['itens_recebidos'] += len(itens)
        try:
            self._sincronizar(fonte, itens, assuntos)
        except Exception as exc:  # noqa: BLE001 - isolate the source failure
            self.stats['fontes_com_erro'] += 1
            mensagem = f'{type(exc).__name__}: {exc}'
            print(f'Source {codigo}: synchronisation failed: {mensagem}', file=sys.stderr)
            if self.apply:
                self._registrar_falha(identificador_fonte, codigo, mensagem)
            return

        self.stats['fontes_concluidas'] += 1
        if self.apply:
            try:
                self.odoo.registrar_sucesso(identificador_fonte)
            except Exception as exc:  # noqa: BLE001 - report and keep other sources running
                self.stats['status_erros'] += 1
                print(
                    f'Source {codigo}: status update failed: {type(exc).__name__}: {exc}',
                    file=sys.stderr,
                )

    def _registrar_falha(self, identificador_fonte: int, codigo: str, mensagem: str) -> None:
        try:
            self.odoo.registrar_erro(identificador_fonte, mensagem)
        except Exception:  # noqa: BLE001 - keep the other sources running
            self.stats['status_erros'] += 1
            print(f'Source {codigo}: could not record the failure', file=sys.stderr)

    def _sincronizar(self, fonte: dict[str, Any], itens: list[Any], assuntos: list[dict[str, Any]]) -> None:
        identificador_fonte = fonte['id']
        identificadores = [item.identificador for item in itens]
        existente = self.odoo.publicacoes_por_identificador(identificador_fonte, identificadores)
        for item in itens:
            classificacao = classificar(item.titulo, item.resumo, assuntos)
            if classificacao.ambigua:
                self.stats['ambiguos'] += 1
            elif classificacao.sem_correspondencia:
                self.stats['sem_assunto'] += 1
            elif classificacao.excluido:
                self.stats['excluidos'] += 1

            atual = existente.get(item.identificador)
            valores = self._valores(item, classificacao.assunto_id)
            valores['x_studio_fontes'] = identificador_fonte

            if classificacao.assunto_id is None:
                if atual is None:
                    # Publications without a valid subject are not imported.
                    self.stats['nao_criados'] += 1
                    continue
                # Existing records keep their current subject.
                valores['x_studio_assunto_1'] = atual.get('x_studio_assunto_1')

            if not self.apply:
                self.stats['existentes'] += atual is not None
                self.stats['novos'] += atual is None
                continue
            if atual is None:
                criado = self._criar(item, valores)
                self.stats['criados'] += 1
                existente[item.identificador] = criado
            else:
                self.stats['existentes'] += 1
                self._atualizar(atual, valores)
                self.stats['atualizados' if _mudou(atual, valores) else 'inalterados'] += 1

    def _criar(self, item, valores: dict[str, Any]) -> dict[str, Any]:
        payload = dict(valores)
        payload['x_studio_primeira_coleta'] = _agora()
        payload['x_studio_ultima_atualizacao'] = payload['x_studio_primeira_coleta']
        try:
            publicacao_id = self.odoo.create_publicacao(payload)
        except RuntimeError as exc:
            confirmado = self.odoo.publicacoes_por_identificador(
                valores['x_studio_fontes'], [item.identificador],
            ).get(item.identificador)
            if confirmado is None:
                raise RuntimeError('Publication creation was not confirmed') from exc
            return confirmado
        confirmado = self.odoo.publicacoes_por_identificador(
            valores['x_studio_fontes'], [item.identificador],
        ).get(item.identificador)
        if confirmado is None or confirmado['id'] != publicacao_id:
            raise RuntimeError('Created publication could not be confirmed')
        return confirmado

    def _atualizar(self, atual: dict[str, Any], valores: dict[str, Any]) -> None:
        changes = _mudou(atual, valores)
        if not changes:
            return
        changes['x_studio_ultima_atualizacao'] = _agora()
        try:
            self.odoo.update_publicacao(atual['id'], changes)
        except RuntimeError:
            pass
        confirmado = self.odoo.publicacoes_por_identificador(
            valores['x_studio_fontes'], [valores['x_studio_identificador_externo']],
        ).get(valores['x_studio_identificador_externo'])
        if confirmado is None or confirmado['id'] != atual['id']:
            raise RuntimeError('Publication update was not confirmed')
        if any(
            normalizar_valor(nome, confirmado.get(nome)) != normalizar_valor(nome, valor)
            for nome, valor in changes.items()
            if nome != 'x_studio_ultima_atualizacao'
        ):
            raise RuntimeError('Publication update values were not confirmed')

    def run(self) -> dict[str, Any]:
        self.odoo.check(write=self.apply)
        fontes = self.odoo.fontes()
        assuntos = self.odoo.assuntos()
        self.stats['fontes'] = len(fontes)
        self.stats['assuntos'] = len(assuntos)
        for fonte in fontes:
            self._processar_fonte(fonte, assuntos)
        return dict(self.stats)
