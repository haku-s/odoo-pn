"""JSON-2 client: metadata validation, normalisation and status writes."""

from __future__ import annotations

import unittest

from intelligence.odoo import (
    ASSUNTOS,
    FONTES,
    PUBLICACOES,
    OdooClient,
    normalizar_valor,
)


SELECAO_TIPO = [['Notícia', 'Notícia'], ['Ato oficial', 'Ato oficial'],
                ['Matéria legislativa', 'Matéria legislativa'],
                ['Contrato público', 'Contrato público'],
                ['Divulgação estatística', 'Divulgação estatística']]
SELECAO_AREA = [['Corporativo', 'Corporativo'], ['Educação', 'Educação'],
                ['Saúde', 'Saúde'], ['Transversal', 'Transversal']]
SELECAO_METODO = [['RSS', 'RSS'], ['API REST', 'API REST'], ['XML', 'XML']]


def campo(tipo, *, string='Campo', readonly=False, store=True, selection=None,
          relation=None, related=None):
    return {'type': tipo, 'string': string, 'readonly': readonly, 'store': store,
            'selection': selection, 'relation': relation, 'related': related}


def metadata_fontes():
    return {
        'x_name': campo('char'),
        'x_studio_codigo_da_fonte_1': campo('char'),
        'x_studio_url': campo('char'),
        'x_studio_metodo_de_coleta': campo('selection', selection=SELECAO_METODO),
        'x_studio_coleta_habilitada': campo('boolean'),
        'x_studio_ultima_coleta_bem_sucedida_1': campo('datetime'),
        'x_studio_ultimo_erro_1': campo('text'),
    }


def metadata_assuntos():
    return {
        'x_name': campo('char'),
        'x_studio_area': campo('selection', selection=SELECAO_AREA),
        'x_studio_termos_de_inclusao': campo('text'),
        'x_studio_termos_de_exclusao': campo('text'),
    }


def metadata_publicacoes():
    return {
        'x_name': campo('char'),
        'x_studio_fontes': campo('many2one', relation='x_fontes'),
        'x_studio_assunto_1': campo('many2one', relation='x_assuntos'),
        'x_studio_identificador_externo': campo('char'),
        'x_studio_url': campo('char'),
        'x_studio_tipo_de_publicacao': campo('selection', selection=SELECAO_TIPO),
        'x_studio_data_de_publicacao': campo('datetime'),
        'x_studio_resumo': campo('text'),
        'x_studio_primeira_coleta': campo('datetime'),
        'x_studio_ultima_atualizacao': campo('datetime'),
        'x_studio_area_1': campo(
            'selection', readonly=True, store=False, selection=SELECAO_AREA,
            related='x_studio_assunto_1.x_studio_area',
        ),
    }


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, respostas=None):
        self.respostas = list(respostas or [])
        self.chamadas = []
        self.headers = {}

    def post(self, url, headers=None, json=None, timeout=None, allow_redirects=False):
        self.chamadas.append({'url': url, 'json': json})
        if not self.respostas:
            raise AssertionError('no queued response')
        return FakeResponse(self.respostas.pop(0))


class ClientTest(unittest.TestCase):
    def cliente(self, respostas=None):
        session = FakeSession(respostas)
        client = OdooClient(
            type('Cfg', (), {
                'odoo_url': 'https://example.test',
                'odoo_database': '',
                'odoo_api_key': 'test-key',
                'require_odoo': lambda write=False: None,
                'request_timeout': (10, 40),
            })(),
            session=session,
        )
        return client, session

    def test_fields_get_requests_related_and_store(self):
        client, session = self.cliente([metadata_fontes(), metadata_assuntos(),
                                        metadata_publicacoes()])
        client.check()
        primeira = session.chamadas[0]
        atributos = primeira['json']['attributes']
        self.assertIn('related', atributos)
        self.assertIn('store', atributos)

    def test_valid_metadata_passes(self):
        client, _ = self.cliente([metadata_fontes(), metadata_assuntos(),
                                  metadata_publicacoes()])
        client.check(write=False)

    def test_missing_metadata_fails(self):
        meta = metadata_publicacoes()
        del meta['x_studio_area_1']
        client, _ = self.cliente([metadata_fontes(), metadata_assuntos(), meta])
        with self.assertRaises(ValueError):
            client.check()

    def test_incorrect_related_field_is_rejected(self):
        meta = metadata_publicacoes()
        meta['x_studio_area_1']['related'] = 'x_studio_fontes.x_studio_area'
        client, _ = self.cliente([metadata_fontes(), metadata_assuntos(), meta])
        with self.assertRaises(ValueError):
            client.check()

    def test_stored_related_field_is_rejected(self):
        meta = metadata_publicacoes()
        meta['x_studio_area_1']['store'] = True
        client, _ = self.cliente([metadata_fontes(), metadata_assuntos(), meta])
        with self.assertRaises(ValueError):
            client.check()

    def test_incorrect_many2one_destination_is_rejected(self):
        meta = metadata_publicacoes()
        meta['x_studio_assunto_1']['relation'] = 'x_fontes'
        client, _ = self.cliente([metadata_fontes(), metadata_assuntos(), meta])
        with self.assertRaises(ValueError):
            client.check()

    def test_invalid_selection_is_rejected(self):
        meta = metadata_publicacoes()
        meta['x_studio_tipo_de_publicacao']['selection'] = [['Outro', 'Outro']]
        client, _ = self.cliente([metadata_fontes(), metadata_assuntos(), meta])
        with self.assertRaises(ValueError):
            client.check()

    def test_write_check_requires_access(self):
        respostas = [metadata_fontes(), metadata_assuntos(), metadata_publicacoes(),
                     True, True, True]
        client, session = self.cliente(respostas)
        client.check(write=True)
        self.assertEqual(len(session.chamadas), 6)
        self.assertTrue(all('/json/2/' in c['url'] for c in session.chamadas))
        operacoes = [c['json'].get('operation') for c in session.chamadas[3:]]
        self.assertEqual(operacoes, ['write', 'write', 'create'])


class NormalizacaoTest(unittest.TestCase):
    def test_many2one_list_is_reduced_to_identifier(self):
        self.assertEqual(normalizar_valor('x_studio_assunto_1', [5, 'Saúde mental']), 5)

    def test_false_and_empty_are_none(self):
        for valor in (False, None, ''):
            self.assertIsNone(normalizar_valor('x_studio_resumo', valor))

    def test_plain_value_is_preserved(self):
        self.assertEqual(normalizar_valor('x_name', 'Título'), 'Título')


class LeituraPublicacoesTest(unittest.TestCase):
    def cliente(self, resposta):
        session = FakeSession([resposta])
        client = OdooClient(
            type('Cfg', (), {
                'odoo_url': 'https://example.test', 'odoo_database': '',
                'odoo_api_key': 'test-key',
                'require_odoo': lambda write=False: None,
                'request_timeout': (10, 40),
            })(),
            session=session,
        )
        return client, session

    def test_normalises_many2one(self):
        client, _ = self.cliente([{
            'id': 7, 'x_studio_fontes': [1, 'Agência'], 'x_studio_identificador_externo': 'a',
            'x_studio_assunto_1': [5, 'Saúde mental'], 'x_studio_resumo': False,
            'x_studio_url': 'https://exemplo/a', 'x_studio_tipo_de_publicacao': 'Notícia',
            'x_studio_data_de_publicacao': '2026-11-20 10:00:00',
            'x_studio_primeira_coleta': '2026-11-20 10:00:00',
            'x_studio_ultima_atualizacao': '2026-11-20 10:00:00',
            'x_name': 'Título',
        }])
        encontrado = client.publicacoes_por_identificador(1, ['a'])['a']
        self.assertEqual(encontrado['x_studio_fontes'], 1)
        self.assertEqual(encontrado['x_studio_assunto_1'], 5)
        self.assertIsNone(encontrado['x_studio_resumo'])

    def test_publication_from_another_source_is_rejected(self):
        client, _ = self.cliente([{
            'id': 7, 'x_studio_fontes': [2, 'Outra'], 'x_studio_identificador_externo': 'a',
            'x_studio_assunto_1': False, 'x_studio_resumo': None,
            'x_studio_url': 'https://exemplo/a', 'x_studio_tipo_de_publicacao': False,
            'x_studio_data_de_publicacao': False, 'x_studio_primeira_coleta': False,
            'x_studio_ultima_atualizacao': False, 'x_name': 'Título',
        }])
        with self.assertRaises(RuntimeError):
            client.publicacoes_por_identificador(1, ['a'])

    def test_read_requests_all_fields(self):
        session = FakeSession([[]])
        client = OdooClient(
            type('Cfg', (), {
                'odoo_url': 'https://example.test', 'odoo_database': '',
                'odoo_api_key': 'test-key',
                'require_odoo': lambda write=False: None,
                'request_timeout': (10, 40),
            })(),
            session=session,
        )
        client.publicacoes_por_identificador(1, ['a'])
        campos = session.chamadas[0]['json']['fields']
        for nome in ('x_name', 'x_studio_url', 'x_studio_resumo', 'x_studio_assunto_1',
                     'x_studio_tipo_de_publicacao', 'x_studio_data_de_publicacao',
                     'x_studio_primeira_coleta', 'x_studio_ultima_atualizacao'):
            self.assertIn(nome, campos)


class StatusTest(unittest.TestCase):
    def cliente(self, resposta=True):
        session = FakeSession([resposta])
        client = OdooClient(
            type('Cfg', (), {
                'odoo_url': 'https://example.test', 'odoo_database': '',
                'odoo_api_key': 'test-key',
                'require_odoo': lambda write=False: None,
                'request_timeout': (10, 40),
            })(),
            session=session,
        )
        return client, session

    def test_error_preserves_last_successful_collection(self):
        client, session = self.cliente()
        client.registrar_erro(1, 'falha')
        valores = session.chamadas[0]['json']['vals']
        self.assertEqual(valores, {'x_studio_ultimo_erro_1': 'falha'})
        self.assertNotIn('x_studio_ultima_coleta_bem_sucedida_1', valores)

    def test_success_clears_error(self):
        client, session = self.cliente()
        client.registrar_sucesso(1)
        valores = session.chamadas[0]['json']['vals']
        self.assertIn('x_studio_ultima_coleta_bem_sucedida_1', valores)
        self.assertIsNone(valores['x_studio_ultimo_erro_1'])

    def test_success_with_false_return_is_rejected(self):
        client, _ = self.cliente(False)
        with self.assertRaises(RuntimeError):
            client.registrar_sucesso(1)

    def test_error_with_false_return_is_rejected(self):
        client, _ = self.cliente(False)
        with self.assertRaises(RuntimeError):
            client.registrar_erro(1, 'falha')

    def test_unexpected_return_is_rejected(self):
        for resposta in (None, 'sim', 1, []):
            client, _ = self.cliente(resposta)
            with self.assertRaises(RuntimeError):
                client.registrar_sucesso(1)

    def test_http_error_is_handled(self):
        session = FakeSession([])
        session.post = lambda *a, **k: (_ for _ in ()).throw(
            __import__('requests').RequestException('queda')
        )
        client = OdooClient(
            type('Cfg', (), {
                'odoo_url': 'https://example.test', 'odoo_database': '',
                'odoo_api_key': 'test-key',
                'require_odoo': lambda write=False: None,
                'request_timeout': (10, 40),
            })(),
            session=session,
        )
        with self.assertRaises(RuntimeError):
            client.registrar_sucesso(1)


class ArquivamentoTest(unittest.TestCase):
    def _client(self, respostas):
        session = FakeSession(respostas)
        client = OdooClient(
            type('Cfg', (), {
                'odoo_url': 'https://example.test', 'odoo_database': '',
                'odoo_api_key': 'test-key',
                'require_odoo': lambda write=False: None,
                'request_timeout': (10, 40),
            })(),
            session=session,
        )
        return client, session

    def test_fontes_consideram_somente_registros_ativos(self):
        client, session = self._client([[]])
        client.fontes()
        self.assertEqual(session.chamadas[0]['json']['domain'], [['x_active', '=', True]])

    def test_assuntos_consideram_somente_registros_ativos(self):
        client, session = self._client([[]])
        client.assuntos()
        self.assertEqual(session.chamadas[0]['json']['domain'], [['x_active', '=', True]])

    def test_publicacoes_incluem_arquivados_na_deduplicacao(self):
        client, session = self._client([[]])
        client.publicacoes_por_identificador(1, ['a'])
        dominio = session.chamadas[0]['json']['domain']
        self.assertNotIn(['x_active', '=', True], dominio)


if __name__ == '__main__':
    unittest.main()
