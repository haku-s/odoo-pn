"""Collect, classify and synchronise publications into Odoo."""

from __future__ import annotations

from collections import Counter
import json
from datetime import datetime, timedelta, timezone
import sys
from typing import Any

import requests

from .classify import classificar, split_terms
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
        self._auditoria: dict[str, Any] | None = None

    @staticmethod
    def _amostra(item) -> dict[str, str]:
        # Official public content only; constrain diagnostic log volume.
        return {
            'identificador': str(item.identificador)[:100],
            'titulo': str(item.titulo)[:140],
            'resumo': str(item.resumo or '')[:180],
        }

    def _auditar_item(self, fonte: dict[str, Any], item, classificacao) -> None:
        if classificacao.assunto_id is not None:
            fonte['classificados'] += 1
            for termo in classificacao.termos_correspondentes:
                key = (classificacao.assunto_id, termo)
                fonte['_termos'][key] += 1
            return
        if classificacao.ambigua:
            categoria = 'ambiguos'
        elif classificacao.excluido:
            categoria = 'excluidos'
        else:
            categoria = 'sem_correspondencia'
        fonte[categoria] += 1
        amostras = fonte['amostras'][categoria]
        if len(amostras) < 3:
            amostras.append(self._amostra(item))

    def _imprimir_auditoria(self) -> None:
        assert self._auditoria is not None
        fontes = []
        for fonte in self._auditoria['fontes']:
            publicavel = {key: value for key, value in fonte.items() if key != '_termos'}
            publicavel['termos_positivos'] = [
                {'assunto_id': assunto_id, 'termo': termo, 'ocorrencias': quantidade}
                for (assunto_id, termo), quantidade in sorted(fonte['_termos'].items())
            ]
            fontes.append(publicavel)
        print(json.dumps({
            'tipo': 'auditoria_classificacao', 'modo': 'preview',
            'assuntos': self._auditoria['assuntos'], 'fontes': fontes,
        }, ensure_ascii=False, sort_keys=True))

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
        auditoria_fonte = None
        if self._auditoria is not None:
            auditoria_fonte = {
                'codigo': codigo, 'estado': 'pendente', 'recebidos': 0,
                'classificados': 0, 'sem_correspondencia': 0,
                'ambiguos': 0, 'excluidos': 0, 'novos_elegiveis': 0,
                'amostras': {'sem_correspondencia': [], 'ambiguos': [], 'excluidos': []},
                '_termos': Counter(),
            }
            self._auditoria['fontes'].append(auditoria_fonte)
        if not fonte.get('x_studio_coleta_habilitada'):
            if auditoria_fonte is not None:
                auditoria_fonte['estado'] = 'desabilitada'
            self.stats['fontes_desabilitadas'] += 1
            return
        if codigo not in COLETORES:
            if auditoria_fonte is not None:
                auditoria_fonte['estado'] = 'nao_implementada'
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
            if auditoria_fonte is not None:
                auditoria_fonte['estado'] = 'falha_na_coleta'
            self.stats['fontes_com_erro'] += 1
            mensagem = f'{type(exc).__name__}: {exc}'
            print(f'Source {codigo}: collection failed: {mensagem}', file=sys.stderr)
            if self.apply:
                self._registrar_falha(identificador_fonte, codigo, mensagem)
            return

        self.stats['fontes_coletadas'] += 1
        self.stats['itens_recebidos'] += len(itens)
        if auditoria_fonte is not None:
            auditoria_fonte['recebidos'] = len(itens)
        try:
            self._sincronizar(fonte, itens, assuntos, auditoria_fonte)
        except Exception as exc:  # noqa: BLE001 - isolate the source failure
            if auditoria_fonte is not None:
                auditoria_fonte['estado'] = 'falha_na_sincronizacao'
            self.stats['fontes_com_erro'] += 1
            mensagem = f'{type(exc).__name__}: {exc}'
            print(f'Source {codigo}: synchronisation failed: {mensagem}', file=sys.stderr)
            if self.apply:
                self._registrar_falha(identificador_fonte, codigo, mensagem)
            return

        self.stats['fontes_concluidas'] += 1
        if auditoria_fonte is not None:
            auditoria_fonte['estado'] = 'concluida'
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

    def _sincronizar(self, fonte: dict[str, Any], itens: list[Any], assuntos: list[dict[str, Any]],
                     auditoria_fonte: dict[str, Any] | None = None) -> None:
        identificador_fonte = fonte['id']
        identificadores = [item.identificador for item in itens]
        existente = self.odoo.publicacoes_por_identificador(identificador_fonte, identificadores)
        for item in itens:
            classificacao = classificar(item.titulo, item.resumo, assuntos)
            if auditoria_fonte is not None:
                self._auditar_item(auditoria_fonte, item, classificacao)
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
                if auditoria_fonte is not None and classificacao.assunto_id is not None and atual is None:
                    auditoria_fonte['novos_elegiveis'] += 1
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
        if not self.apply:
            # Terms come from the live Odoo records, not from test fixtures.
            def contar_termos(value: object) -> int | None:
                try:
                    return len(split_terms(value))
                except ValueError:
                    return None

            self._auditoria = {
                'assuntos': [
                    {
                        'id': assunto['id'], 'nome': assunto['x_name'],
                        'termos_inclusao': contar_termos(assunto.get('x_studio_termos_de_inclusao')),
                        'termos_exclusao': contar_termos(assunto.get('x_studio_termos_de_exclusao')),
                    }
                    for assunto in assuntos
                ],
                'fontes': [],
            }
        for fonte in fontes:
            self._processar_fonte(fonte, assuntos)
        if self._auditoria is not None:
            self._imprimir_auditoria()
        return dict(self.stats)
