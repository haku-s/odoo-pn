"""JSON-2 client: metadata validation, normalisation and status writes."""

from __future__ import annotations

import json
import unittest

from intelligence.odoo import (
    OdooClient,
    OdooHTTPError,
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def iter_content(self, chunk_size):
        yield json.dumps(self._payload).encode()


class FakeSession:
    def __init__(self, respostas=None):
        self.respostas = list(respostas or [])
        self.chamadas = []
        self.headers = {}

    def post(self, url, headers=None, json=None, timeout=None, allow_redirects=False, stream=False):
        self.chamadas.append({'url': url, 'json': json})
        if not self.respostas:
            raise AssertionError('no queued response')
        payload = self.respostas.pop(0)
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)


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


AVATAR = 'x_test_avatar'
AVATAR_IMAGE = 'Ynl0ZXM='
AVATAR_ARCH = '<form><sheet><field name="x_test_avatar" widget="image" class="oe_avatar ml-3 mr-3"/></sheet></form>'
PUBLICATION = {
    'id': 7,
    'x_studio_fontes': [1, 'Source'],
    'x_studio_identificador_externo': 'article-1',
    'x_studio_url': 'https://example.test/article-1',
}


class AvatarTest(unittest.TestCase):
    def client(self, *responses, field=AVATAR, arch=AVATAR_ARCH, metadata=None):
        cfg = type('Cfg', (), {
            'odoo_url': 'https://example.test', 'odoo_database': '',
            'odoo_api_key': 'test-key', 'require_odoo': lambda self: None,
            'request_timeout': (10, 40), 'avatar_field': field,
        })()
        model = 'x_publicacoes'
        session = FakeSession([
            metadata if metadata is not None else {
                AVATAR: campo('binary', string='Avatar'), 'x_active': campo('boolean'),
            },
            {'model': model, 'id': 8, 'models': {model: [AVATAR]}, 'arch': arch},
            *responses,
        ])
        return OdooClient(cfg, session=session), session

    def test_report_probes_real_binary_shapes_without_exposing_values(self):
        client, session = self.client(
            True, 2, [{'id': 1, AVATAR: False}], 1,
            [{'id': 2, AVATAR: {'content': AVATAR_IMAGE, 'size': 5}}],
        )
        report = client.avatar_info()
        self.assertEqual(report['field'], AVATAR)
        self.assertEqual(report['widget'], 'image')
        self.assertEqual(report['type'], 'binary')
        self.assertTrue(report['writable'])
        self.assertEqual(report['populated'], 1)
        self.assertEqual(report['empty_representation'], 'false')
        self.assertEqual(report['populated_representation'], 'base64_object')
        self.assertNotIn(AVATAR_IMAGE, str(report))
        self.assertFalse(any(call['url'].endswith(('/create', '/write')) for call in session.chamadas))

    def test_explicit_configuration_is_required_for_image_writes(self):
        client, session = self.client(field=None)
        self.assertIsNone(client.avatar_field(write=True))
        self.assertEqual(session.chamadas, [])

    def test_read_only_avatar_resolution_needs_no_configured_field(self):
        client, session = self.client(12, field=None)
        self.assertEqual(client.avatar_field(), AVATAR)
        self.assertEqual(client.avatar_missing_count(AVATAR), 12)
        self.assertEqual([call['url'].rsplit('/', 1)[-1] for call in session.chamadas],
                         ['fields_get', 'get_view', 'search_count'])

    def test_wrong_ambiguous_and_read_only_fields_are_rejected(self):
        both = AVATAR_ARCH.replace('</sheet>',
                                   '<field name="x_other" string="Avatar" widget="image"/></sheet>')
        for name, arch, metadata in (
            ('x_other', AVATAR_ARCH, None),
            (AVATAR, both, {AVATAR: campo('binary', string='Avatar'), 'x_other': campo('binary')}),
            (AVATAR, AVATAR_ARCH, {AVATAR: campo('binary', string='Avatar', readonly=True)}),
        ):
            with self.subTest(name=name, arch=arch):
                client, _ = self.client(True, field=name, arch=arch, metadata=metadata)
                with self.assertRaises(ValueError):
                    client.avatar_field(write=True)

    def test_missing_page_uses_active_empty_domain_and_id_order(self):
        client, session = self.client([PUBLICATION])
        self.assertEqual(client.avatar_missing_page(AVATAR, after_id=4, limit=2), [PUBLICATION])
        params = session.chamadas[-1]['json']
        self.assertIn(['x_active', '=', True], params['domain'])
        self.assertIn([AVATAR, '=', False], params['domain'])
        self.assertIn(['id', '>', 4], params['domain'])
        self.assertEqual(params['order'], 'id asc')
        self.assertEqual(params['limit'], 2)
        self.assertNotIn(AVATAR, params['fields'])

    def test_write_preserves_filled_or_archived_record(self):
        client, session = self.client(True, True, [])
        self.assertFalse(client.write_avatar(7, AVATAR, AVATAR_IMAGE, expected=PUBLICATION))
        self.assertFalse(any(call['url'].endswith('/write') for call in session.chamadas))
        self.assertIn(['x_active', '=', True], session.chamadas[-1]['json']['domain'])
        self.assertIn([AVATAR, '=', False], session.chamadas[-1]['json']['domain'])

    def test_write_checks_identity_before_update(self):
        client, session = self.client(True, True, [{**PUBLICATION, 'x_studio_url': 'other'}])
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            client.write_avatar(7, AVATAR, AVATAR_IMAGE, expected=PUBLICATION)
        self.assertFalse(any(call['url'].endswith('/write') for call in session.chamadas))

    def test_write_only_avatar_and_confirms_binary_content(self):
        for content in (AVATAR_IMAGE, {'content': AVATAR_IMAGE, 'size': 5}):
            with self.subTest(content_type=type(content).__name__):
                client, session = self.client(True, True, [PUBLICATION], True, [{'id': 7, AVATAR: content}])
                self.assertTrue(client.write_avatar(7, AVATAR, AVATAR_IMAGE, expected=PUBLICATION))
                self.assertEqual(session.chamadas[-2]['json']['vals'], {AVATAR: AVATAR_IMAGE})
                self.assertEqual(session.chamadas[-2]['json']['ids'], [7])

    def test_write_failure_is_unconfirmed_if_read_back_differs(self):
        client, _ = self.client(True, True, [PUBLICATION], False, [{'id': 7, AVATAR: 'YQ=='}])
        with self.assertRaisesRegex(RuntimeError, 'confirmed'):
            client.write_avatar(7, AVATAR, AVATAR_IMAGE, expected=PUBLICATION)

    def test_write_readback_recovers_uncertain_response(self):
        client, session = self.client(True, True, [PUBLICATION], False, [{'id': 7, AVATAR: AVATAR_IMAGE}])
        self.assertTrue(client.write_avatar(7, AVATAR, AVATAR_IMAGE, expected=PUBLICATION))
        self.assertEqual(sum(call['url'].endswith('/write') for call in session.chamadas), 1)

    def test_create_rejection_needs_known_odoo_error_and_hides_response_details(self):
        for status, name, rejected in (
            (422, 'odoo.exceptions.ValidationError', True),
            (422, 'werkzeug.exceptions.UnprocessableEntity', False),
            (500, 'odoo.exceptions.ValidationError', False),
        ):
            with self.subTest(status=status, name=name):
                client, session = self.client()
                response = FakeResponse({'name': name, 'message': AVATAR_IMAGE})
                response.status_code = status
                session.respostas = [response]
                with self.assertRaises(OdooHTTPError) as caught:
                    client.create_publicacao({AVATAR: AVATAR_IMAGE})
                self.assertIs(caught.exception.validation_rejected, rejected)
                self.assertNotIn(AVATAR_IMAGE, str(caught.exception))

    def test_exact_url_lookup_checks_archived_and_rejects_unrequested_rows(self):
        client, session = self.client()
        url = PUBLICATION['x_studio_url']
        session.respostas = [[PUBLICATION], [{**PUBLICATION, 'x_studio_fontes': [3, 'Other']}]]
        self.assertEqual(client.publicacoes_por_urls([1, 2], [url]), {url})
        self.assertIs(session.chamadas[0]['json']['context']['active_test'], False)
        self.assertNotIn(['x_active', '=', True], session.chamadas[0]['json']['domain'])
        with self.assertRaises(RuntimeError):
            client.publicacoes_por_urls([1, 2], [url])

    def test_source_inventory_includes_archived_and_rejects_duplicate_codes(self):
        client, session = self.client()
        row = {'id': 1, 'x_active': False, 'x_studio_codigo_da_fonte_1': 'source',
               'x_studio_metodo_de_coleta': 'RSS'}
        session.respostas = [[row], [row, {**row, 'id': 2, 'x_active': True}]]
        self.assertEqual(client.fontes(include_archived=True), [row])
        self.assertEqual(session.chamadas[0]['json']['domain'], [])
        self.assertIn('x_active', session.chamadas[0]['json']['fields'])
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            client.fontes(include_archived=True)


if __name__ == '__main__':
    unittest.main()
