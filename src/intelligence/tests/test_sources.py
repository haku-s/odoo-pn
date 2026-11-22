"""Source collectors: parsing, validation and date handling (no network)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from intelligence import sources


class FakeResponse:
    def __init__(self, *, content=None, payload=None, status=200):
        self.content = content
        self._payload = payload
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class FakeSession:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def get(self, url, params=None, timeout=None, allow_redirects=False):
        self.chamadas.append((url, params))
        if not self.respostas:
            raise AssertionError('no queued response')
        return self.respostas.pop(0)


RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item><title>Nota um</title><link>https://agenciabrasil.ebc.com.br/a</link>
<pubDate>Sat, 21 Nov 2026 15:12:00 -0300</pubDate>
<description>&lt;p&gt;Resumo &amp; detalhe&lt;/p&gt;</description></item>
<item><title>Nota dois</title><link>https://agenciabrasil.ebc.com.br/b</link>
<pubDate>Mon, 01 Jan 2024 10:00:00 -0300</pubDate>
<description>Antiga</description></item>
</channel></rss>"""

EBC_FEED = 'https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml'


def agency_feed(content=None, *, status=200):
    with patch.object(
        sources, 'read_public', return_value=(content, EBC_FEED),
        side_effect=RuntimeError(f'Source HTTP {status}') if status != 200 else None,
    ):
        return sources.coletar_agencia_brasil(None, EBC_FEED, datetime(2026, 11, 1, tzinfo=UTC), 10)

CAMARA = {'dados': [
    {'id': 2655695, 'siglaTipo': 'PL', 'numero': 5756, 'ano': 2026,
     'ementa': 'Amplia prazo de recursos.', 'dataApresentacao': '2026-11-19T16:05',
     'uri': 'https://dadosabertos.camara.leg.br/api/v2/proposicoes/2655695'},
]}

SENADO = [
    {'codigoMateria': 176161, 'identificacao': 'PL 5726/2026',
     'ementa': 'Acrescenta artigo.', 'dataApresentacao': '2026-11-03',
     'urlDocumento': 'https://legis.senado.gov.br/doc'},
]

SENADO_SEM_URL = [
    {'codigoMateria': 176161, 'identificacao': 'PL 5726/2026',
     'ementa': 'Acrescenta artigo.', 'dataApresentacao': '2026-11-03'},
]


def camara_pagina(quantidade, inicio=1):
    return {'dados': [
        {'id': inicio + indice, 'siglaTipo': 'PL', 'numero': indice + 1, 'ano': 2026,
         'ementa': f'Proposição {inicio + indice}.',
         'dataApresentacao': '2026-11-19T16:05',
         'uri': f'https://dadosabertos.camara.leg.br/api/v2/proposicoes/{inicio + indice}'}
        for indice in range(quantidade)
    ]}


def ibge_pagina(quantidade, inicio=1):
    return {'count': quantidade, 'items': [
        {'id': inicio + indice, 'tipo': 'Notícia', 'titulo': f'Notícia {inicio + indice}',
         'introducao': 'Texto.', 'data_publicacao': '19/11/2026 03:20:00',
         'link': f'https://agenciadenoticias.ibge.gov.br/{inicio + indice}'}
        for indice in range(quantidade)
    ]}


def camara_ids(ids):
    return {'dados': [
        {'id': identificador, 'siglaTipo': 'PL', 'numero': 1, 'ano': 2026,
         'ementa': f'Proposição {identificador}.',
         'dataApresentacao': '2026-11-19T16:05',
         'uri': f'https://dadosabertos.camara.leg.br/api/v2/proposicoes/{identificador}'}
        for identificador in ids
    ]}


def ibge_ids(ids):
    return {'count': len(ids), 'items': [
        {'id': identificador, 'tipo': 'Notícia', 'titulo': f'Notícia {identificador}',
         'introducao': 'Texto.', 'data_publicacao': '19/11/2026 03:20:00',
         'link': f'https://agenciadenoticias.ibge.gov.br/{identificador}'}
        for identificador in ids
    ]}

IBGE = {'count': 1, 'items': [
    {'id': 48702, 'tipo': 'Notícia', 'titulo': 'Live do IBGE',
     'introducao': 'Texto introdutório.', 'data_publicacao': '19/11/2026 03:20:00',
     'link': 'https://agenciadenoticias.ibge.gov.br/48702'},
]}


class DateTest(unittest.TestCase):
    def test_rfc822(self):
        self.assertEqual(
            sources._data_hora('Sat, 21 Nov 2026 15:12:00 -0300'),
            '2026-11-21 18:12:00',
        )

    def test_iso_without_zone_uses_brasilia(self):
        self.assertEqual(
            sources._data_hora('2026-11-19T16:05'),
            '2026-11-19 19:05:00',
        )

    def test_brazilian_format(self):
        self.assertEqual(
            sources._data_hora('19/11/2026 03:20:00'),
            '2026-11-19 06:20:00',
        )

    def test_invalid(self):
        with self.assertRaises(sources.RespostaInvalida):
            sources._data_hora('ontem')


class AgenciaBrasilTest(unittest.TestCase):
    def test_parse(self):
        itens = agency_feed(RSS)
        self.assertEqual(len(itens), 1)
        item = itens[0]
        self.assertEqual(item.titulo, 'Nota um')
        self.assertEqual(item.identificador, 'https://agenciabrasil.ebc.com.br/a')
        self.assertEqual(item.data_publicacao, '2026-11-21 18:12:00')
        self.assertEqual(item.tipo, 'Notícia')
        self.assertIn('Resumo', item.resumo)

    def test_dtd_rejected(self):
        with self.assertRaises(sources.RespostaInvalida):
            agency_feed(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "b">]><r/>')

    def test_foreign_domain_rejected(self):
        with self.assertRaises(ValueError):
            sources._url_oficial('agencia_brasil', 'https://example.com/feed.xml')


class CamaraTest(unittest.TestCase):
    def test_parse(self):
        session = FakeSession([FakeResponse(payload=CAMARA)])
        itens = sources.coletar_camara(
            session, 'https://dadosabertos.camara.leg.br/api/v2/proposicoes',
            datetime(2026, 11, 1), 10,
        )
        self.assertEqual(len(itens), 1)
        item = itens[0]
        self.assertEqual(item.identificador, '2655695')
        self.assertEqual(item.titulo, 'PL 5756/2026')
        self.assertEqual(item.data_publicacao, '2026-11-19 19:05:00')
        self.assertEqual(item.tipo, 'Matéria legislativa')


class SenadoTest(unittest.TestCase):
    def test_parse(self):
        session = FakeSession([FakeResponse(payload=SENADO)])
        itens = sources.coletar_senado(
            session, 'https://legis.senado.leg.br/dadosabertos/processo',
            datetime(2026, 11, 1), 10,
        )
        self.assertEqual(itens[0].identificador, '176161')
        self.assertEqual(itens[0].titulo, 'PL 5726/2026')
        self.assertEqual(itens[0].data_publicacao, '2026-11-03 03:00:00')
        self.assertEqual(itens[0].url, 'https://legis.senado.gov.br/doc')

    def test_canonical_url_when_document_missing(self):
        session = FakeSession([FakeResponse(payload=SENADO_SEM_URL)])
        itens = sources.coletar_senado(
            session, 'https://legis.senado.leg.br/dadosabertos/processo',
            datetime(2026, 11, 1), 10,
        )
        self.assertEqual(
            itens[0].url,
            'https://www25.senado.leg.br/web/atividade/materias/-/materia/176161',
        )


class FakeCamaraPaginada:
    """Simulates offset pagination for the Chamber API, with optional overlap."""

    def __init__(self, ids, sobreposicao=0, repetir=False):
        self.ids = list(ids)
        self.sobreposicao = sobreposicao
        self.repetir = repetir
        self.chamadas = []

    def get(self, url, params=None, timeout=None, allow_redirects=False):
        self.chamadas.append(dict(params))
        pagina = params['pagina']
        tamanho = params['itens']
        if self.repetir:
            fatia = self.ids[:tamanho]
        else:
            inicio = (pagina - 1) * tamanho
            fatia = self.ids[max(0, inicio - self.sobreposicao):inicio + tamanho]
        return FakeResponse(payload=camara_ids(fatia))


class FakeIbgePaginado:
    """Simulates offset pagination for the IBGE API, with optional overlap."""

    def __init__(self, ids, sobreposicao=0, repetir=False):
        self.ids = list(ids)
        self.sobreposicao = sobreposicao
        self.repetir = repetir
        self.chamadas = []

    def get(self, url, params=None, timeout=None, allow_redirects=False):
        self.chamadas.append(dict(params))
        pagina = params['page']
        tamanho = params['qtd']
        if self.repetir:
            fatia = self.ids[:tamanho]
        else:
            inicio = (pagina - 1) * tamanho
            fatia = self.ids[max(0, inicio - self.sobreposicao):inicio + tamanho]
        return FakeResponse(payload=ibge_ids(fatia))


class PaginacaoDeslocamentoTest(unittest.TestCase):
    URL_CAMARA = 'https://dadosabertos.camara.leg.br/api/v2/proposicoes'
    URL_IBGE = 'https://servicodados.ibge.gov.br/api/v3/noticias'

    def test_limite_10(self):
        session = FakeCamaraPaginada(range(1, 51))
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 10)
        self.assertEqual(len(itens), 10)
        self.assertEqual(session.chamadas[0]['itens'], 10)

    def test_limite_100(self):
        session = FakeCamaraPaginada(range(1, 201))
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 100)
        self.assertEqual(len(itens), 100)
        self.assertEqual(len(session.chamadas), 1)

    def test_limite_110_em_duas_paginas(self):
        session = FakeCamaraPaginada(range(1, 300))
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 110)
        self.assertEqual(len(itens), 110)
        self.assertEqual(len({item.identificador for item in itens}), 110)
        self.assertEqual([c['pagina'] for c in session.chamadas], [1, 2])
        self.assertEqual([c['itens'] for c in session.chamadas], [100, 100])

    def test_limite_250_em_tres_paginas(self):
        session = FakeCamaraPaginada(range(1, 400))
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 250)
        self.assertEqual(len(itens), 250)
        self.assertEqual(len({item.identificador for item in itens}), 250)
        self.assertEqual([c['pagina'] for c in session.chamadas], [1, 2, 3])

    def test_pagina_final_incompleta(self):
        session = FakeCamaraPaginada(range(1, 51))
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 250)
        self.assertEqual(len(itens), 50)
        self.assertEqual(len(session.chamadas), 1)

    def test_sobreposicao_parcial(self):
        session = FakeCamaraPaginada(range(1, 300), sobreposicao=40)
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 150)
        self.assertEqual(len(itens), 150)
        self.assertEqual(len({item.identificador for item in itens}), 150)

    def test_repeticao_integral_de_paginas(self):
        session = FakeCamaraPaginada(range(1, 300), repetir=True)
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 250)
        self.assertEqual(len(itens), 100)
        self.assertEqual(len({item.identificador for item in itens}), 100)
        self.assertEqual(len(session.chamadas), 2)

    def test_resposta_vazia(self):
        session = FakeCamaraPaginada([])
        self.assertEqual(
            sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 250), [],
        )

    def test_identificadores_duplicados_na_mesma_pagina(self):
        session = FakeSession([FakeResponse(payload=camara_ids([1, 1, 2]))])
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 100)
        self.assertEqual([item.identificador for item in itens], ['1', '2'])

    def test_ibge_limite_110_em_duas_paginas(self):
        session = FakeIbgePaginado(range(1, 300))
        itens = sources.coletar_ibge(session, self.URL_IBGE, datetime(2026, 11, 1), 110)
        self.assertEqual(len(itens), 110)
        self.assertEqual(len({item.identificador for item in itens}), 110)
        self.assertEqual([c['qtd'] for c in session.chamadas], [100, 100])
        self.assertEqual([c['page'] for c in session.chamadas], [1, 2])

    def test_ibge_limite_10(self):
        session = FakeIbgePaginado(range(1, 51))
        itens = sources.coletar_ibge(session, self.URL_IBGE, datetime(2026, 11, 1), 10)
        self.assertEqual(len(itens), 10)
        self.assertEqual(session.chamadas[0]['qtd'], 10)

    def test_ibge_repeticao_integral(self):
        session = FakeIbgePaginado(range(1, 300), repetir=True)
        itens = sources.coletar_ibge(session, self.URL_IBGE, datetime(2026, 11, 1), 250)
        self.assertEqual(len(itens), 100)
        self.assertEqual(len(session.chamadas), 2)

    def test_ibge_sends_qtd_and_page(self):
        session = FakeSession([FakeResponse(payload=ibge_pagina(10))])
        sources.coletar_ibge(session, self.URL_IBGE, datetime(2026, 11, 1), 10)
        parametros = session.chamadas[0][1]
        self.assertEqual(parametros['qtd'], 10)
        self.assertEqual(parametros['page'], 1)
        self.assertNotIn('quantidade', parametros)

    def test_camara_repeated_pages_do_not_duplicate(self):
        session = FakeSession([
            FakeResponse(payload=camara_ids(range(1, 101))),
            FakeResponse(payload=camara_ids(range(1, 101))),
        ])
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 250)
        self.assertEqual(len(itens), 100)
        self.assertEqual(len({item.identificador for item in itens}), 100)
        self.assertEqual(len(session.chamadas), 2)

    def test_camara_repeated_response_without_progress(self):
        session = FakeSession([
            FakeResponse(payload=camara_ids(range(1, 101))),
            FakeResponse(payload=camara_ids(range(1, 101))),
            FakeResponse(payload=camara_ids(range(101, 201))),
        ])
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 250)
        self.assertEqual(len(itens), 100)
        self.assertEqual(len(session.chamadas), 2)

    def test_camara_partial_overlap(self):
        session = FakeSession([
            FakeResponse(payload=camara_ids(range(1, 101))),
            FakeResponse(payload=camara_ids(range(61, 161))),
        ])
        itens = sources.coletar_camara(session, self.URL_CAMARA, datetime(2026, 11, 1), 150)
        self.assertEqual(len(itens), 150)
        self.assertEqual(len({item.identificador for item in itens}), 150)

    def test_ibge_repeated_pages_do_not_duplicate(self):
        session = FakeSession([
            FakeResponse(payload=ibge_ids(range(1, 101))),
            FakeResponse(payload=ibge_ids(range(1, 101))),
        ])
        itens = sources.coletar_ibge(session, self.URL_IBGE, datetime(2026, 11, 1), 250)
        self.assertEqual(len(itens), 100)
        self.assertEqual(len({item.identificador for item in itens}), 100)

    def test_ibge_repeated_response_without_progress(self):
        session = FakeSession([
            FakeResponse(payload=ibge_ids(range(1, 101))),
            FakeResponse(payload=ibge_ids(range(1, 101))),
            FakeResponse(payload=ibge_ids(range(101, 201))),
        ])
        itens = sources.coletar_ibge(session, self.URL_IBGE, datetime(2026, 11, 1), 250)
        self.assertEqual(len(itens), 100)
        self.assertEqual(len(session.chamadas), 2)


class IbgeTest(unittest.TestCase):
    def test_parse(self):
        session = FakeSession([FakeResponse(payload=IBGE)])
        itens = sources.coletar_ibge(
            session, 'https://servicodados.ibge.gov.br/api/v3/noticias',
            datetime(2026, 11, 1), 10,
        )
        self.assertEqual(itens[0].identificador, '48702')
        self.assertEqual(itens[0].tipo, 'Notícia')
        self.assertEqual(itens[0].data_publicacao, '2026-11-19 06:20:00')

    def test_non_news_has_no_type(self):
        dados = {'items': [dict(IBGE['items'][0], tipo='Vídeo')]}
        session = FakeSession([FakeResponse(payload=dados)])
        itens = sources.coletar_ibge(
            session, 'https://servicodados.ibge.gov.br/api/v3/noticias',
            datetime(2026, 11, 1), 10,
        )
        self.assertIsNone(itens[0].tipo)


class RobustaTest(unittest.TestCase):
    def test_rss_without_items(self):
        rss = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        self.assertEqual(agency_feed(rss), [])

    def test_http_500(self):
        with self.assertRaises(sources.RespostaInvalida):
            agency_feed(status=500)

    def test_http_401(self):
        session = FakeSession([FakeResponse(status=401)])
        with self.assertRaises(sources.RespostaInvalida):
            sources.coletar_ibge(
                session, 'https://servicodados.ibge.gov.br/api/v3/noticias',
                datetime(2026, 11, 1), 10)

    def test_camara_empty_response(self):
        session = FakeSession([FakeResponse(payload={'dados': []})])
        self.assertEqual(sources.coletar_camara(
            session, 'https://dadosabertos.camara.leg.br/api/v2/proposicoes',
            datetime(2026, 11, 1), 10), [])

    def test_camara_invalid_structure(self):
        session = FakeSession([FakeResponse(payload={'dados': 'x'})])
        with self.assertRaises(sources.RespostaInvalida):
            sources.coletar_camara(
                session, 'https://dadosabertos.camara.leg.br/api/v2/proposicoes',
                datetime(2026, 11, 1), 10)

    def test_ibge_invalid_record(self):
        dados = {'items': [{'id': 0, 'titulo': 'x', 'link': 'https://x'}]}
        session = FakeSession([FakeResponse(payload=dados)])
        with self.assertRaises(sources.RespostaInvalida):
            sources.coletar_ibge(
                session, 'https://servicodados.ibge.gov.br/api/v3/noticias',
                datetime(2026, 11, 1), 10)


class RegistryTest(unittest.TestCase):
    def test_unimplemented_source(self):
        with self.assertRaises(sources.FonteNaoImplementada):
            sources.coletar('dou_inlabs', FakeSession([]), 'https://inlabs.in.gov.br/', datetime.now(UTC), 10)


if __name__ == '__main__':
    unittest.main()
