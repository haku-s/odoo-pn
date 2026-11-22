"""Collect, classify and synchronise publications into Odoo."""

from __future__ import annotations

from collections import Counter
import json
from datetime import datetime, timedelta, timezone
from itertools import batched
import sys
from typing import Any
from zoneinfo import ZoneInfo

import requests

from . import images
from .camara_media import eligible_photo, is_article
from .classify import classificar, split_terms
from .odoo import OdooClient, OdooHTTPError, SELECTIONS, normalizar_valor
from .sources import COLETORES, coletar, rss_publisher


def _publisher(source: dict[str, Any]) -> str | None:
    code = source['x_studio_codigo_da_fonte_1']
    if code == 'agencia_brasil' or code not in COLETORES and source.get('x_studio_metodo_de_coleta') == 'RSS':
        return rss_publisher(source.get('x_studio_url'))
    return None


def _agora() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _mudou(antigo: dict[str, Any], valores: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields whose normalised stored value differs."""
    return {
        nome: valor for nome, valor in valores.items()
        if normalizar_valor(nome, antigo.get(nome)) != normalizar_valor(nome, valor)
    }


class Synchronizer:
    def __init__(self, cfg, odoo: OdooClient, session: requests.Session, apply: bool = False,
                 *, show_classification_details: bool = True):
        self.cfg = cfg
        self.odoo = odoo
        self.session = session
        self.apply = apply
        self.show_classification_details = show_classification_details
        self.stats = Counter()
        self._auditoria: dict[str, Any] | None = None
        self.source_audit: dict[str, Counter[str]] = {}
        self._publishers: dict[str, list[int]] = {}
        self._seen_urls: dict[str, set[str]] = {}
        self._avatar: str | None = None

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

    def _processar_fonte(
        self, fonte: dict[str, Any], assuntos: list[dict[str, Any]], start: datetime, end: datetime,
    ) -> None:
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
        if codigo not in COLETORES and _publisher(fonte) is None:
            if auditoria_fonte is not None:
                auditoria_fonte['estado'] = 'nao_implementada'
            self.stats['fontes_nao_implementadas'] += 1
            print(f'Source {codigo}: no collector implemented', file=sys.stderr)
            return
        try:
            itens = coletar(
                codigo, self.session, fonte.get('x_studio_url'), start,
                self.cfg.max_items_per_source,
                method=fonte.get('x_studio_metodo_de_coleta'),
                timeout=getattr(self.cfg, 'request_timeout', (10, 40)), until=end,
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
        counts: Counter[str] = Counter(dict.fromkeys((
            'collected', 'classified', 'discarded', 'existing', 'duplicates',
            'images_identified', 'images_available', 'images_written',
            'images_enriched', 'without_available_image', 'image_failures',
        ), 0))
        self.source_audit[codigo] = counts
        counts['collected'] = len(itens)
        if auditoria_fonte is not None:
            auditoria_fonte['recebidos'] = len(itens)
            auditoria_fonte['collection_counts'] = counts
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
        code = fonte['x_studio_codigo_da_fonte_1']
        counts = self.source_audit[code]
        identificadores = [item.identificador for item in itens]
        existente = self.odoo.publicacoes_por_identificador(identificador_fonte, identificadores)
        publisher = _publisher(fonte)
        other_sources = [source_id for source_id in self._publishers.get(publisher or '', ())
                         if source_id != identificador_fonte]
        stored_urls: set[str] = set()
        seen_urls: set[str] = set()
        if publisher and other_sources:
            seen_urls = self._seen_urls.setdefault(publisher, set())
            for urls in batched(dict.fromkeys(item.url for item in itens), 100, strict=False):
                stored_urls.update(self.odoo.publicacoes_por_urls(other_sources, list(urls)))
        for item in itens:
            classificacao = classificar(item.titulo, item.resumo, assuntos)
            counts['classified' if classificacao.assunto_id is not None else 'discarded'] += 1
            if auditoria_fonte is not None:
                self._auditar_item(auditoria_fonte, item, classificacao)
            if classificacao.ambigua:
                self.stats['ambiguos'] += 1
            elif classificacao.sem_correspondencia:
                self.stats['sem_assunto'] += 1
            elif classificacao.excluido:
                self.stats['excluidos'] += 1

            atual = existente.get(item.identificador)
            if atual is not None:
                counts['existing'] += 1
            if atual is None and (item.url in stored_urls or item.url in seen_urls):
                counts['duplicates'] += 1
                self.stats['duplicates'] += 1
                continue
            valores = self._valores(item, classificacao.assunto_id)
            valores['x_studio_fontes'] = identificador_fonte

            if classificacao.assunto_id is None:
                if atual is None:
                    # Publications without a valid subject are not imported.
                    self.stats['nao_criados'] += 1
                    continue
                # Existing records keep their current subject.
                valores['x_studio_assunto_1'] = atual.get('x_studio_assunto_1')

            image = self._image_for(item, atual, code, counts)
            if not self.apply:
                if auditoria_fonte is not None and classificacao.assunto_id is not None and atual is None:
                    auditoria_fonte['novos_elegiveis'] += 1
                self.stats['existentes'] += atual is not None
                self.stats['novos'] += atual is None
                if publisher and other_sources:
                    seen_urls.add(item.url)
                continue
            if atual is None:
                criado = self._criar(item, valores, image=image, code=code, counts=counts)
                self.stats['criados'] += 1
                existente[item.identificador] = criado
            else:
                self.stats['existentes'] += 1
                self._atualizar(atual, valores)
                self.stats['atualizados' if _mudou(atual, valores) else 'inalterados'] += 1
                if image and self._avatar:
                    try:
                        identity = {**atual, 'x_studio_url': item.url}
                        if self.odoo.write_avatar(atual['id'], self._avatar, image, expected=identity):
                            counts['images_enriched'] += 1
                            self.stats['images_enriched'] += 1
                            self._image_written(counts)
                    except (ValueError, RuntimeError) as exc:
                        self._image_failed(code, counts, 'write', exc)

    def _image_for(self, item, current, code: str, counts: Counter[str]) -> str | None:
        candidate, credit = item.image_url, item.image_credit
        if candidate:
            counts['images_identified'] += 1
        if not is_article(item.url):
            counts['without_available_image'] += 1
            return None
        if not self._avatar:
            return None
        stage = 'check'
        try:
            if current and not self.odoo.avatar_missing(current['id'], self._avatar):
                return None
            if not self.apply:
                if eligible_photo(item.url, candidate, credit):
                    counts['images_available'] += 1
                return None
            stage = 'download'
            if not eligible_photo(item.url, candidate, credit):
                found = images.image_reference(item.url, timeout=self.cfg.request_timeout)
                if not found:
                    counts['without_available_image'] += 1
                    return None
                candidate, credit = found
                if not item.image_url:
                    counts['images_identified'] += 1
            if not candidate or not credit:
                return None
            counts['images_available'] += 1
            return images.prepare_image(
                candidate, article_url=item.url, credit=credit, timeout=self.cfg.request_timeout,
            )
        except (ValueError, RuntimeError) as exc:
            self._image_failed(code, counts, stage, exc)
            return None

    def _image_written(self, counts: Counter[str]) -> None:
        counts['images_written'] += 1
        self.stats['images_written'] += 1

    def _image_failed(self, code: str, counts: Counter[str], stage: str, exc: Exception) -> None:
        counts['image_failures'] += 1
        self.stats['image_failures'] += 1
        if counts['image_failures'] <= 3:
            print(f'Source {code}: image {stage} failed: {exc}', file=sys.stderr)

    def _criar(self, item, valores: dict[str, Any], *, image: str | None,
               code: str, counts: Counter[str]) -> dict[str, Any]:
        payload = dict(valores)
        payload['x_studio_primeira_coleta'] = _agora()
        payload['x_studio_ultima_atualizacao'] = payload['x_studio_primeira_coleta']
        if image and self._avatar:
            payload[self._avatar] = image
        try:
            publicacao_id = self.odoo.create_publicacao(payload)
        except RuntimeError as exc:
            confirmado = self.odoo.publicacoes_por_identificador(
                valores['x_studio_fontes'], [item.identificador],
            ).get(item.identificador)
            if confirmado is None:
                if image and self._avatar and isinstance(exc, OdooHTTPError) and exc.validation_rejected:
                    self._image_failed(code, counts, 'create', exc)
                    return self._criar(item, valores, image=None, code=code, counts=counts)
                raise RuntimeError('Publication creation was not confirmed') from exc
        else:
            confirmado = self.odoo.publicacoes_por_identificador(
                valores['x_studio_fontes'], [item.identificador],
            ).get(item.identificador)
            if confirmado is None or confirmado['id'] != publicacao_id:
                raise RuntimeError('Created publication could not be confirmed')
        if image and self._avatar:
            try:
                self.odoo.confirm_avatar(confirmado['id'], self._avatar, image)
                self._image_written(counts)
            except (ValueError, RuntimeError) as exc:
                self._image_failed(code, counts, 'confirmation', exc)
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

    def run(self, *, preview_source: str | None = None) -> dict[str, Any]:
        if preview_source and self.apply:
            raise ValueError('Testing one source is only available in preview mode')
        start = datetime.now(ZoneInfo('America/Sao_Paulo')).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        self.odoo.check(write=self.apply)
        fontes = self.odoo.fontes()
        if preview_source:
            selected = next((source for source in fontes if source['x_studio_codigo_da_fonte_1'] == preview_source), None)
            if selected is None:
                raise ValueError('Requested preview source was not found among active sources')
            fontes = [{**selected, 'x_studio_coleta_habilitada': True}]
        assuntos = self.odoo.assuntos()
        if any(_publisher(source) and source.get('x_studio_coleta_habilitada') for source in fontes):
            for source in self.odoo.fontes(include_archived=True):
                if publisher := _publisher(source):
                    self._publishers.setdefault(publisher, []).append(source['id'])
        if getattr(self.cfg, 'avatar_field', None):
            try:
                self._avatar = self.odoo.avatar_field(write=self.apply)
            except (ValueError, RuntimeError) as exc:
                print(f'Avatar preflight blocked: {exc}', file=sys.stderr)
                self.stats['avatar_preflight_blocked'] += 1
        self.stats['fontes'] = len(fontes)
        self.stats['assuntos'] = len(assuntos)
        if not self.apply and self.show_classification_details:
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
            self._processar_fonte(fonte, assuntos, start, end)
        if self._auditoria is not None:
            self._imprimir_auditoria()
        coverage = {
            key: sum(source[key] for source in self.source_audit.values())
            for key in ('images_identified', 'images_available', 'images_written', 'without_available_image')
        }
        return {'collection_date': start.date().isoformat(), **self.stats, **coverage}
