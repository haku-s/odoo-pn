"""Synchronisation: deduplication, idempotence, filtering and status handling."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from intelligence import sync
from intelligence.__main__ import _todas_as_fontes_falharam
from intelligence.sources import Item


def registro(identificador, **extras):
    base = {
        'id': 1, 'x_studio_fontes': 1, 'x_studio_identificador_externo': identificador,
        'x_name': 'Título', 'x_studio_url': f'https://exemplo/{identificador}',
        'x_studio_tipo_de_publicacao': 'Notícia',
        'x_studio_data_de_publicacao': '2026-11-20 10:00:00',
        'x_studio_assunto_1': 5, 'x_studio_resumo': None,
        'x_studio_primeira_coleta': '2026-11-20 10:00:00',
        'x_studio_ultima_atualizacao': '2026-11-20 10:00:00',
    }
    base.update(extras)
    return base


class FakeOdoo:
    """Reproduces the normalised records returned by the real JSON-2 client."""

    def __init__(self, fontes, assuntos, publicacoes=None):
        self._fontes = fontes
        self._assuntos = assuntos
        self._publicacoes = publicacoes or {}
        self.criadas = []
        self.atualizadas = []
        self.status = []
        self.falhas = []

    def check(self, write=False):
        self.write_check = write

    def fontes(self, *, include_archived=False):
        return self._fontes

    def assuntos(self):
        return self._assuntos

    def publicacoes_por_identificador(self, fonte_id, identificadores):
        return {
            valor['x_studio_identificador_externo']: dict(valor)
            for (fid, _), valor in self._publicacoes.items()
            if fid == fonte_id and valor['x_studio_identificador_externo'] in identificadores
        }

    def publicacoes_por_urls(self, source_ids, urls):
        return {
            row['x_studio_url']
            for (source_id, _), row in self._publicacoes.items()
            if source_id in source_ids and row['x_studio_url'] in urls
        }

    def create_publicacao(self, valores):
        identificador = (valores['x_studio_fontes'], valores['x_studio_identificador_externo'])
        registro_novo = {'id': 100 + len(self.criadas), **valores}
        self.criadas.append(registro_novo)
        self._publicacoes[identificador] = registro_novo
        return registro_novo['id']

    def update_publicacao(self, publicacao_id, valores):
        for registro in self._publicacoes.values():
            if registro['id'] == publicacao_id:
                registro.update(valores)
                self.atualizadas.append((publicacao_id, valores))
                return
        raise RuntimeError('missing')

    def registrar_sucesso(self, fonte_id):
        self.status.append((fonte_id, True, None))

    def registrar_erro(self, fonte_id, erro):
        self.falhas.append((fonte_id, erro))


def fonte(identificador, codigo, habilitada=True,
          url='https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml'):
    return {
        'id': identificador,
        'x_name': codigo,
        'x_studio_codigo_da_fonte_1': codigo,
        'x_studio_metodo_de_coleta': 'RSS',
        'x_studio_coleta_habilitada': habilitada,
        'x_studio_url': url,
    }


ASSUNTOS = [
    {'id': 5, 'x_name': 'Saúde mental', 'x_studio_area': 'Saúde',
     'x_studio_termos_de_inclusao': 'saúde mental', 'x_studio_termos_de_exclusao': ''},
    {'id': 6, 'x_name': 'Aprendizagem', 'x_studio_area': 'Educação',
     'x_studio_termos_de_inclusao': 'alfabetização', 'x_studio_termos_de_exclusao': ''},
]


def item(identificador, titulo='Saúde mental pública', resumo=''):
    return Item(identificador, titulo, f'https://exemplo/{identificador}',
                '2026-11-20 10:00:00', resumo, 'Notícia')


class FakeOdooFalhaNaCriacao(FakeOdoo):
    """Simulates an uncertain create: the call fails but the record exists."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.criacoes = 0

    def create_publicacao(self, valores):
        self.criacoes += 1
        identificador = (valores['x_studio_fontes'], valores['x_studio_identificador_externo'])
        if identificador not in self._publicacoes:
            self._publicacoes[identificador] = {'id': 999, **valores}
        raise RuntimeError('uncertain create')


class FakeOdooFalhaNaSincronizacao(FakeOdoo):
    def create_publicacao(self, valores):
        raise RuntimeError('write failed')


class FakeOdooStatusSeletivo(FakeOdoo):
    """Fails the operational status write for the given source ids."""

    def __init__(self, *args, falhar=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.falhar = set(falhar)

    def registrar_sucesso(self, fonte_id):
        if fonte_id in self.falhar:
            raise RuntimeError('status write failed')
        self.status.append((fonte_id, True, None))


class SyncTest(unittest.TestCase):
    def run_sync(self, odoo, itens, apply):
        with patch.object(sync, 'coletar', return_value=itens) as coletar:
            resultado = sync.Synchronizer(self.cfg(), odoo, object(), apply=apply).run()
        self.chamadas = coletar
        return resultado

    def cfg(self):
        class Cfg:
            max_age_days = 30
            max_items_per_source = 100
        return Cfg()

    # ------------------------------------------------------------ write safety
    def test_news_lookback_crosses_midnight_without_changing_legislative_window(self):
        clock = datetime(2026, 11, 20, 2, 59, 59, tzinfo=UTC)
        odoo = FakeOdoo([fonte(1, 'agencia_brasil'), fonte(2, 'camara')], ASSUNTOS)
        with patch.object(sync, 'datetime', wraps=datetime) as mocked_clock:
            mocked_clock.now.side_effect = lambda zone: clock.astimezone(zone)
            stats = self.run_sync(odoo, [], apply=False)
        self.assertEqual(stats['collection_date'], '2026-11-19')
        self.assertEqual(self.chamadas.call_count, 2)
        news, legislative = self.chamadas.call_args_list
        self.assertEqual(news.args[3].astimezone(UTC), clock - timedelta(hours=72))
        self.assertEqual(news.kwargs['until'].astimezone(UTC), clock)
        self.assertEqual(legislative.args[3].astimezone(UTC), datetime(2026, 11, 19, 3, tzinfo=UTC))
        self.assertEqual(legislative.kwargs['until'].astimezone(UTC), datetime(2026, 11, 20, 3, tzinfo=UTC))
        mocked_clock.now.assert_called_once()

    def test_replay_uses_inclusive_brasilia_dates_and_skips_legislative_sources(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil'), fonte(2, 'camara'), fonte(3, 'ibge')], ASSUNTOS)
        with (patch.object(sync, 'coletar', return_value=[]) as collect,
              patch.object(sync, 'datetime', wraps=datetime) as clock):
            clock.now.side_effect = lambda zone: datetime(2026, 11, 23, 13, tzinfo=zone)
            runner = sync.Synchronizer(self.cfg(), odoo, object(), show_classification_details=False)
            runner.run(period=(date(2026, 11, 20), date(2026, 11, 21)))
        self.assertEqual([call.args[0] for call in collect.call_args_list], ['agencia_brasil', 'ibge'])
        for call in collect.call_args_list:
            self.assertEqual(call.args[3].astimezone(UTC), datetime(2026, 11, 20, 3, tzinfo=UTC))
            self.assertEqual(call.kwargs['until'].astimezone(UTC), datetime(2026, 11, 22, 3, tzinfo=UTC))
        self.assertEqual(runner.source_states['camara'], 'outside_replay_scope')
        self.assertEqual(odoo.status, [])

    def test_preview_recovers_previous_day_item_after_an_outage_without_writes(self):
        from intelligence import sources

        source = fonte(1, 'agencia_brasil')
        odoo = FakeOdoo([source], ASSUNTOS)
        feed = ('<rss version="2.0"><channel><item><title>Saúde mental</title>'
                '<link>https://agenciabrasil.ebc.com.br/saude/noticia/2026-11/example</link>'
                '<pubDate>2026-11-19T23:40:00-03:00</pubDate></item></channel></rss>').encode()
        with (patch.object(sync, 'datetime', wraps=datetime) as clock,
              patch.object(sources, 'read_public', side_effect=[RuntimeError('unavailable'),
                           (feed, source['x_studio_url'])])):
            clock.now.side_effect = [datetime(2026, 11, 19, 23, 55, tzinfo=sync.BRASILIA),
                                     datetime(2026, 11, 20, 0, 50, tzinfo=sync.BRASILIA)]
            failed = sync.Synchronizer(self.cfg(), odoo, object(), show_classification_details=False).run()
            recovered = sync.Synchronizer(self.cfg(), odoo, object(), show_classification_details=False).run()
        self.assertEqual((failed['fontes_com_erro'], recovered['novos']), (1, 1))
        self.assertEqual(odoo.status, [])
        self.assertEqual(odoo._publicacoes, {})

    def test_targeted_preview_can_read_one_disabled_source_without_writes(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil'), fonte(2, 'bcb_sgs', habilitada=False)], ASSUNTOS)
        with patch.object(sync, 'coletar', return_value=[]) as collect:
            stats = sync.Synchronizer(self.cfg(), odoo, object()).run(preview_source='bcb_sgs')
        self.assertEqual(stats['fontes_concluidas'], 1)
        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[0], 'bcb_sgs')
        self.assertFalse(odoo._fontes[1]['x_studio_coleta_habilitada'])
        self.assertEqual(odoo.criadas + odoo.atualizadas + odoo.status, [])
        with self.assertRaises(ValueError):
            sync.Synchronizer(self.cfg(), odoo, object(), apply=True).run(preview_source='bcb_sgs')

    def test_preview_does_not_write(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a')], apply=False)
        self.assertEqual(odoo.criadas, [])
        self.assertEqual(odoo.atualizadas, [])
        self.assertEqual(odoo.status, [])
        self.assertEqual(odoo.falhas, [])
        self.assertEqual(stats['novos'], 1)
        self.assertTrue(odoo.write_check is False)

    def test_apply_creates(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(len(odoo.criadas), 1)
        self.assertEqual(stats['criados'], 1)
        valores = odoo.criadas[0]
        self.assertEqual(valores['x_studio_assunto_1'], 5)
        self.assertEqual(valores['x_studio_fontes'], 1)
        self.assertIn('x_studio_primeira_coleta', valores)

    def test_area_related_field_is_never_written(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        self.run_sync(odoo, [item('a')], apply=True)
        for valores in odoo.criadas:
            self.assertNotIn('x_studio_area_1', valores)

    # ---------------------------------------------------------- idempotence
    def test_repeated_run_does_not_duplicate(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        self.run_sync(odoo, [item('a')], apply=True)
        odoo.criadas.clear()
        stats = self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(odoo.criadas, [])
        self.assertEqual(stats['existentes'], 1)

    def test_unchanged_run_produces_no_update(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS,
                        {(1, 'a'): registro('a')})
        stats = self.run_sync(odoo, [item('a', titulo='Título')], apply=True)
        self.assertEqual(odoo.atualizadas, [])
        self.assertEqual(stats['inalterados'], 1)

    def test_update_preserves_first_collection(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        primeira = '2026-11-01 08:00:00'
        odoo._publicacoes[(1, 'a')] = registro(
            'a', x_name='Título antigo',
            x_studio_primeira_coleta=primeira, x_studio_ultima_atualizacao=primeira,
        )
        stats = self.run_sync(odoo, [item('a', titulo='Título novo')], apply=True)
        self.assertEqual(len(odoo.atualizadas), 1)
        registro_atual = odoo._publicacoes[(1, 'a')]
        self.assertEqual(registro_atual['x_studio_primeira_coleta'], primeira)
        self.assertEqual(registro_atual['x_name'], 'Título novo')
        self.assertEqual(stats['atualizados'], 1)

    def test_many2one_false_is_equal_to_empty(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS,
                        {(1, 'a'): registro('a', x_studio_assunto_1=None)})
        self.run_sync(odoo, [item('a', titulo='Título', resumo='')], apply=True)
        # The stored subject is absent and the item is unclassified: no update.
        self.assertEqual(odoo.atualizadas, [])
        self.assertEqual(odoo.criadas, [])

    def test_uncertain_create_does_not_duplicate(self):
        odoo = FakeOdooFalhaNaCriacao([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(odoo.criacoes, 1)
        self.assertEqual(len(odoo._publicacoes), 1)
        self.assertEqual(stats['criados'], 1)

    def test_failed_update_confirmation_is_reported(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS,
                        {(1, 'a'): registro('a', x_name='Antigo')})

        def falha(publicacao_id, valores):
            raise RuntimeError('write failed')

        odoo.update_publicacao = falha
        stats = self.run_sync(odoo, [item('a', titulo='Novo')], apply=True)
        self.assertEqual(stats['fontes_com_erro'], 1)
        self.assertEqual(len(odoo.falhas), 1)
        self.assertEqual(odoo.atualizadas, [])
        self.assertEqual(odoo._publicacoes[(1, 'a')]['x_name'], 'Antigo')

    # ------------------------------------------------------------- filtering
    def test_unrelated_content_is_not_created(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a', titulo='Esporte olímpico')], apply=True)
        self.assertEqual(odoo.criadas, [])
        self.assertEqual(stats['sem_assunto'], 1)
        self.assertEqual(stats['nao_criados'], 1)

    def test_ambiguous_content_is_not_created(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(
            odoo, [item('a', titulo='Saúde mental e alfabetização')], apply=True,
        )
        self.assertEqual(odoo.criadas, [])
        self.assertEqual(stats['ambiguos'], 1)
        self.assertEqual(stats['nao_criados'], 1)

    def test_excluded_content_is_not_created(self):
        assuntos = [dict(ASSUNTOS[0], x_studio_termos_de_exclusao='descontinuado')]
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], assuntos)
        stats = self.run_sync(
            odoo, [item('a', titulo='Saúde mental descontinuado')], apply=True,
        )
        self.assertEqual(odoo.criadas, [])
        self.assertEqual(stats['excluidos'], 1)
        self.assertEqual(stats['nao_criados'], 1)

    def test_existing_unrelated_record_is_preserved(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS,
                        {(1, 'a'): registro('a', x_studio_assunto_1=6)})
        stats = self.run_sync(odoo, [item('a', titulo='Esporte olímpico')], apply=True)
        self.assertEqual(odoo.criadas, [])
        self.assertEqual(odoo._publicacoes[(1, 'a')]['x_studio_assunto_1'], 6)
        self.assertEqual(stats['existentes'], 1)

    def test_preview_keeps_unrelated_content_read_only(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a', titulo='Esporte olímpico')], apply=False)
        self.assertEqual(odoo.criadas, [])
        self.assertEqual(odoo.atualizadas, [])
        self.assertEqual(stats['nao_criados'], 1)

    # --------------------------------------------------------------- sources
    def test_disabled_source_is_skipped(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil', habilitada=False)], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(self.chamadas.call_count, 0)
        self.assertEqual(stats['fontes_desabilitadas'], 1)

    def test_unimplemented_source_is_reported(self):
        odoo = FakeOdoo([fonte(11, 'dou_inlabs', url='https://inlabs.in.gov.br/')], ASSUNTOS)
        stats = self.run_sync(odoo, [], apply=True)
        self.assertEqual(stats['fontes_nao_implementadas'], 1)

    def test_empty_response_is_not_a_failure(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [], apply=True)
        self.assertEqual(stats['fontes_coletadas'], 1)
        self.assertEqual(stats.get('fontes_com_erro', 0), 0)
        self.assertEqual(odoo.status, [(1, True, None)])

    # ------------------------------------------------------------- status
    def test_collection_failure_keeps_last_success(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        with patch.object(sync, 'coletar', side_effect=RuntimeError('queda')):
            preview = sync.Synchronizer(self.cfg(), odoo, object(), apply=False).run()
            self.assertEqual(odoo.status, [])
            self.assertEqual(odoo.falhas, [])
            apply = sync.Synchronizer(self.cfg(), odoo, object(), apply=True).run()
        self.assertEqual(preview['fontes_com_erro'], 1)
        self.assertEqual(apply['fontes_com_erro'], 1)
        self.assertEqual(odoo.status, [])
        self.assertEqual(len(odoo.falhas), 1)

    def test_success_is_recorded_only_after_synchronisation(self):
        odoo = FakeOdooFalhaNaSincronizacao([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(stats['fontes_com_erro'], 1)
        self.assertEqual(stats['fontes_coletadas'], 1)
        self.assertEqual(odoo.status, [])
        self.assertEqual(len(odoo.falhas), 1)

    def test_success_is_recorded_after_successful_synchronisation(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(odoo.status, [(1, True, None)])
        self.assertEqual(odoo.falhas, [])

    def test_success_is_counted_after_synchronisation(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(stats['fontes_concluidas'], 1)
        self.assertEqual(stats.get('fontes_com_erro', 0), 0)
        self.assertEqual(stats.get('status_erros', 0), 0)

    def test_synchronisation_failure_is_not_counted_as_success(self):
        odoo = FakeOdooFalhaNaSincronizacao([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(stats.get('fontes_coletadas', 0), 1)
        self.assertEqual(stats.get('fontes_concluidas', 0), 0)
        self.assertEqual(stats['fontes_com_erro'], 1)

    def test_download_failure_is_not_counted_as_success(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil')], ASSUNTOS)
        with patch.object(sync, 'coletar', side_effect=RuntimeError('queda')):
            stats = sync.Synchronizer(self.cfg(), odoo, object(), apply=True).run()
        self.assertEqual(stats.get('fontes_coletadas', 0), 0)
        self.assertEqual(stats.get('fontes_concluidas', 0), 0)
        self.assertEqual(stats['fontes_com_erro'], 1)

    def test_two_sources_one_completed_and_one_failed(self):
        odoo = FakeOdoo([fonte(1, 'agencia_brasil'), fonte(2, 'ibge')], ASSUNTOS)
        with patch.object(sync, 'coletar', side_effect=[[item('a')], RuntimeError('queda')]):
            stats = sync.Synchronizer(self.cfg(), odoo, object(), apply=True).run()
        self.assertEqual(stats['fontes_concluidas'], 1)
        self.assertEqual(stats['fontes_com_erro'], 1)
        self.assertEqual(len(odoo.criadas), 1)

    def test_two_sources_both_with_synchronisation_failure(self):
        class FalhaTodas(FakeOdoo):
            def create_publicacao(self, valores):
                raise RuntimeError('write failed')

        odoo = FalhaTodas([fonte(1, 'agencia_brasil'), fonte(2, 'ibge')], ASSUNTOS)
        with patch.object(sync, 'coletar', side_effect=[[item('a')], [item('b')]]):
            stats = sync.Synchronizer(self.cfg(), odoo, object(), apply=True).run()
        self.assertEqual(stats.get('fontes_concluidas', 0), 0)
        self.assertEqual(stats['fontes_com_erro'], 2)

    def test_status_failure_does_not_stop_other_sources(self):
        odoo = FakeOdooStatusSeletivo(
            [fonte(1, 'agencia_brasil'), fonte(2, 'ibge')], ASSUNTOS, falhar={1},
        )
        with patch.object(sync, 'coletar', side_effect=[[item('a')], [item('b')]]):
            stats = sync.Synchronizer(self.cfg(), odoo, object(), apply=True).run()
        self.assertEqual(stats['fontes_concluidas'], 2)
        self.assertEqual(stats['status_erros'], 1)
        self.assertEqual(stats.get('fontes_com_erro', 0), 0)
        self.assertEqual(odoo.status, [(2, True, None)])

    def test_error_recording_failure_is_isolated(self):
        class FalhaStatus(FakeOdoo):
            def registrar_erro(self, fonte_id, erro):
                raise RuntimeError('status write failed')

        odoo = FalhaStatus([fonte(1, 'agencia_brasil')], ASSUNTOS)
        with patch.object(sync, 'coletar', side_effect=RuntimeError('queda')):
            stats = sync.Synchronizer(self.cfg(), odoo, object(), apply=True).run()
        self.assertEqual(stats['fontes_com_erro'], 1)
        self.assertEqual(stats['status_erros'], 1)

    def test_synchronisation_failure_and_error_recording_failure(self):
        class FalhaDupla(FakeOdoo):
            def create_publicacao(self, valores):
                raise RuntimeError('write failed')

            def registrar_erro(self, fonte_id, erro):
                raise RuntimeError('status write failed')

        odoo = FalhaDupla([fonte(1, 'agencia_brasil')], ASSUNTOS)
        stats = self.run_sync(odoo, [item('a')], apply=True)
        self.assertEqual(stats['fontes_com_erro'], 1)
        self.assertEqual(stats['status_erros'], 1)

    def test_operational_failure_in_first_source_keeps_second(self):
        class FalhaStatusPrimeira(FakeOdoo):
            def registrar_erro(self, fonte_id, erro):
                if fonte_id == 1:
                    raise RuntimeError('status write failed')
                self.falhas.append((fonte_id, erro))

        odoo = FalhaStatusPrimeira([fonte(1, 'agencia_brasil'), fonte(2, 'ibge')], ASSUNTOS)
        with patch.object(sync, 'coletar', side_effect=[RuntimeError('queda'), [item('b')]]):
            stats = sync.Synchronizer(self.cfg(), odoo, object(), apply=True).run()
        self.assertEqual(stats['fontes_com_erro'], 1)
        self.assertEqual(stats['status_erros'], 1)
        self.assertEqual(stats['fontes_concluidas'], 1)
        self.assertEqual(odoo.status, [(2, True, None)])


class ExitStatusTest(unittest.TestCase):
    def test_all_attempted_sources_failed(self):
        self.assertTrue(_todas_as_fontes_falharam(
            {'fontes_concluidas': 0, 'fontes_com_erro': 2, 'fontes_coletadas': 2},
        ))

    def test_single_source_failed_after_a_successful_download(self):
        self.assertTrue(_todas_as_fontes_falharam(
            {'fontes_concluidas': 0, 'fontes_com_erro': 1, 'fontes_coletadas': 1},
        ))

    def test_partial_failure_is_not_total(self):
        self.assertFalse(_todas_as_fontes_falharam(
            {'fontes_concluidas': 1, 'fontes_com_erro': 1, 'fontes_coletadas': 2},
        ))

    def test_empty_response_is_not_a_failure(self):
        self.assertFalse(_todas_as_fontes_falharam(
            {'fontes_concluidas': 1, 'fontes_coletadas': 1},
        ))

    def test_no_source_configured_is_not_a_failure(self):
        self.assertFalse(_todas_as_fontes_falharam({'fontes_desabilitadas': 3}))


class CliExitCodeTest(unittest.TestCase):
    def _main(self, stats):
        from intelligence import __main__ as cli
        ambiente = {
            'ODOO_URL': 'https://example.test',
            'ODOO_API_KEY': 'test-key',
        }
        with patch.dict('os.environ', ambiente, clear=False), \
             patch.object(cli, 'OdooClient') as cliente, \
             patch.object(cli, 'Synchronizer') as sincronizador:
            cliente.return_value = object()
            sincronizador.return_value.run.return_value = stats
            sincronizador.return_value.source_audit = {}
            sincronizador.return_value.source_states = {}
            sincronizador.return_value.source_errors = {}
            with redirect_stdout(io.StringIO()) as stdout:
                code = cli.main(['apply', '--summary-only'])
            self.output = json.loads(stdout.getvalue())
            return code

    def test_cli_returns_0_on_success(self):
        self.assertEqual(self._main({'fontes_concluidas': 1, 'fontes_coletadas': 1}), 0)

    def test_cli_returns_0_without_publications(self):
        self.assertEqual(self._main({'fontes_concluidas': 1, 'fontes_coletadas': 1}), 0)

    def test_cli_returns_1_when_all_sources_fail(self):
        self.assertEqual(self._main(
            {'fontes_concluidas': 0, 'fontes_com_erro': 1, 'fontes_coletadas': 1},
        ), 1)

    def test_cli_returns_0_on_partial_failure(self):
        self.assertEqual(self._main(
            {'fontes_concluidas': 1, 'fontes_com_erro': 1, 'fontes_coletadas': 2},
        ), 0)
        self.assertEqual(self.output['status'], 'partial')
        self.assertIn('sources', self.output)

    def test_cli_returns_1_on_status_failure(self):
        self.assertEqual(self._main(
            {'fontes_concluidas': 1, 'fontes_coletadas': 1, 'status_erros': 1},
        ), 1)

    def test_cli_returns_0_without_eligible_sources(self):
        self.assertEqual(self._main({'fontes_desabilitadas': 3}), 0)


if __name__ == '__main__':
    unittest.main()


class SyncImagesTest(unittest.TestCase):
    ARTICLE = 'https://www.camara.leg.br/noticias/12345-sample'
    PHOTO = 'https://www.camara.leg.br/midias/image/photo.png'
    FIELD = 'x_confirmed_avatar'
    CREDIT = 'João Silva / Câmara dos Deputados'

    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        self.eligible = Item('a', 'Saúde mental', self.ARTICLE, None, None, 'Notícia', self.PHOTO, self.CREDIT)
        self.cfg = SimpleNamespace(
            max_age_days=30, max_items_per_source=100, request_timeout=(2, 3),
            avatar_field=self.FIELD,
        )
        self.odoo = FakeOdoo([fonte(1, 'camara_saude', url='https://www.camara.leg.br/noticias/rss/dinamico/SAUDE')], ASSUNTOS)
        self.odoo.avatar_field = Mock(return_value=self.FIELD)
        self.odoo.avatar_missing = Mock(return_value=True)
        self.odoo.confirm_avatar = Mock()
        self.odoo.write_avatar = Mock(return_value=True)

    def run_sync(self, items, *, apply=True, error=None):
        with patch.object(sync, 'coletar', return_value=items), patch(
            'intelligence.images.prepare_image', return_value='YWJj', side_effect=error,
        ) as download:
            result = sync.Synchronizer(self.cfg, self.odoo, object(), apply=apply).run()
        return result, download

    def test_create_with_image_confirms_payload(self):
        result, download = self.run_sync([self.eligible])
        self.assertEqual(self.odoo.criadas[0][self.FIELD], 'YWJj')
        self.odoo.confirm_avatar.assert_called_once_with(100, self.FIELD, 'YWJj')
        self.assertEqual(result['images_written'], 1)
        download.assert_called_once()

    def test_preview_and_unclassified_never_download(self):
        from dataclasses import replace
        for candidate, apply in ((self.eligible, False), (replace(self.eligible, titulo='Skip'), True)):
            with self.subTest(apply=apply):
                _, download = self.run_sync([candidate], apply=apply)
                download.assert_not_called()
                self.assertEqual(self.odoo.criadas, [])
                self.odoo.write_avatar.assert_not_called()

    def test_image_failure_does_not_discard_publication(self):
        result, _ = self.run_sync([self.eligible], error=ValueError('Invalid image'))
        self.assertEqual(result['image_failures'], 1)
        self.assertNotIn(self.FIELD, self.odoo.criadas[0])

    def test_different_feed_reference_can_use_authorized_article_image(self):
        from dataclasses import replace

        candidate = replace(self.eligible, image_credit=None)
        with patch('intelligence.images.image_reference', return_value=(self.PHOTO, self.CREDIT)) as metadata:
            result, download = self.run_sync([candidate])
        metadata.assert_called_once()
        self.assertEqual(download.call_args.args[0], self.PHOTO)
        self.assertEqual(result['images_written'], 1)

    def test_existing_avatar_is_preserved_and_empty_avatar_is_enriched(self):
        from dataclasses import replace
        existing = replace(self.eligible, data_publicacao='2026-11-20 10:00:00', titulo='Título')
        self.odoo._publicacoes[(1, 'a')] = registro('a', x_studio_url=self.ARTICLE)
        self.odoo.avatar_missing.return_value = False
        _, download = self.run_sync([existing])
        download.assert_not_called()
        self.odoo.write_avatar.assert_not_called()

        self.odoo.avatar_missing.return_value = True
        result, download = self.run_sync([existing])
        download.assert_called_once()
        self.assertEqual(result['images_enriched'], 1)
        self.assertEqual(self.odoo.atualizadas, [])
        self.assertEqual(self.odoo.write_avatar.call_args.args, (1, self.FIELD, 'YWJj'))
        self.odoo.avatar_missing.return_value = False
        _, download = self.run_sync([existing])
        download.assert_not_called()
        self.odoo.write_avatar.assert_called_once()

    def test_explicit_image_create_rejection_falls_back_to_text_once(self):
        from intelligence.odoo import OdooHTTPError, PUBLICACOES

        original = self.odoo.create_publicacao

        def create(values):
            if self.FIELD in values:
                raise OdooHTTPError(PUBLICACOES, 'create', 422, validation_rejected=True)
            return original(values)

        with patch.object(self.odoo, 'create_publicacao', side_effect=create) as write:
            counts, _ = self.run_sync([self.eligible])
        self.assertEqual(write.call_count, 2)
        self.assertEqual(counts['criados'], 1)
        self.assertEqual(counts['image_failures'], 1)
        self.assertNotIn(self.FIELD, self.odoo.criadas[0])

    def test_unconfirmed_image_write_does_not_discard_created_publication(self):
        self.odoo.confirm_avatar.side_effect = RuntimeError('Image readback mismatch')
        counts, _ = self.run_sync([self.eligible])
        self.assertEqual(counts['criados'], 1)
        self.assertEqual(counts['image_failures'], 1)
        self.assertEqual(counts.get('images_written', 0), 0)


class CrossFeedDuplicatesTest(unittest.TestCase):
    def test_same_publisher_exact_url_matches_even_across_archived_sources(self):
        feeds = [
            fonte(1, 'camara_saude', url='https://www.camara.leg.br/noticias/rss/dinamico/SAUDE'),
            fonte(2, 'camara_educacao', url='https://www.camara.leg.br/noticias/rss/dinamico/EDUCACAO'),
        ]
        article = 'https://www.camara.leg.br/noticias/123-title'
        news = Item('new', 'Saúde mental', article, None, None, 'Notícia')
        odoo = FakeOdoo(feeds[:1], ASSUNTOS, {(2, 'old'): registro('old', x_studio_fontes=2, x_studio_url=article)})
        cfg = SyncTest().cfg()
        with patch.object(odoo, 'fontes', side_effect=lambda include_archived=False: feeds if include_archived else feeds[:1]), \
             patch.object(sync, 'coletar', return_value=[news]):
            counts = sync.Synchronizer(cfg, odoo, object(), apply=True).run()
        self.assertEqual(odoo.criadas, [])
        self.assertEqual(counts['duplicates'], 1)

    def test_same_publisher_cross_feed_duplicate_in_preview(self):
        feeds = [
            fonte(1, 'camara_saude', url='https://www.camara.leg.br/noticias/rss/dinamico/SAUDE'),
            fonte(2, 'camara_educacao', url='https://www.camara.leg.br/noticias/rss/dinamico/EDUCACAO'),
        ]
        article = 'https://www.camara.leg.br/noticias/123-title'
        first = Item('first', 'Saúde mental', article, None, None, 'Notícia')
        second = Item('second', 'Saúde mental', article, None, None, 'Notícia')
        odoo = FakeOdoo(feeds, ASSUNTOS)
        with patch.object(sync, 'coletar', side_effect=[[first], [second]]):
            counts = sync.Synchronizer(SyncTest().cfg(), odoo, object()).run()
        self.assertEqual(counts['novos'], 1)
        self.assertEqual(counts['duplicates'], 1)
        self.assertEqual(odoo.criadas + odoo.atualizadas + odoo.status, [])


class InspectImagesTest(unittest.TestCase):
    def test_inspect_lists_source_state_and_avatar_metadata_without_writes(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json

        from intelligence import __main__ as cli

        row = {**fonte(1, 'agencia_brasil'), 'x_active': True}
        output = StringIO()
        with patch.dict('os.environ', {'ODOO_URL': 'https://example.test', 'ODOO_API_KEY': 'test'}), \
             patch.object(cli, 'OdooClient') as client, redirect_stdout(output):
            odoo = client.return_value
            odoo.fontes.return_value = [row]
            odoo.assuntos.return_value = []
            odoo.selection.return_value = ()
            odoo.avatar_info.return_value = {'field': 'x_verified', 'type': 'binary'}
            self.assertEqual(cli.main(['inspect']), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data['avatar']['field'], 'x_verified')
        self.assertEqual(data['sources'][0]['approved_feed'], row['x_studio_url'])
        self.assertFalse(odoo.create_publicacao.called or odoo.update_publicacao.called or odoo.write_avatar.called)

    def test_public_summary_omits_instance_details(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json

        from intelligence import __main__ as cli

        output = StringIO()
        with patch.dict('os.environ', {'ODOO_URL': 'https://example.test', 'ODOO_API_KEY': 'test'}), \
             patch.object(cli, 'OdooClient') as client, redirect_stdout(output):
            odoo = client.return_value
            odoo.fontes.return_value = [{**fonte(1, 'private_source'), 'x_active': True}]
            odoo.assuntos.return_value = []
            odoo.avatar_info.return_value = {'status': 'available', 'field': 'x_private', 'type': 'binary'}
            self.assertEqual(cli.main(['inspect', '--summary-only']), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data['avatar'], {'status': 'available', 'type': 'binary'})
        self.assertNotIn('sources', data)
        self.assertNotIn('x_private', output.getvalue())
