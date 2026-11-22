"""Deterministic classification rules."""

from __future__ import annotations

import unittest

from intelligence.classify import classificar, fold, split_terms


def assunto(identificador, nome, inclusao, exclusao='', area='Saúde'):
    return {
        'id': identificador,
        'x_name': nome,
        'x_studio_area': area,
        'x_studio_termos_de_inclusao': inclusao,
        'x_studio_termos_de_exclusao': exclusao,
    }


class FoldTest(unittest.TestCase):
    def test_normalises_case_and_accents(self):
        self.assertEqual(fold('Saúde Mental'), 'saude mental')
        self.assertEqual(fold('Ação'), 'acao')

    def test_split_terms_one_per_line(self):
        self.assertEqual(split_terms('a\n b \n\nc'), ('a', 'b', 'c'))
        self.assertEqual(split_terms(None), ())


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.assuntos = [
            assunto(1, 'Saúde mental', 'saúde mental\ncognição'),
            assunto(2, 'Aprendizagem', 'alfabetização\naprendizagem', area='Educação'),
            assunto(3, 'Transversal', 'política pública', area='Transversal'),
        ]

    def test_unique_match(self):
        resultado = classificar('Programa de saúde mental', '', self.assuntos)
        self.assertEqual(resultado.assunto_id, 1)
        self.assertFalse(resultado.ambigua)

    def test_match_on_summary(self):
        resultado = classificar('Educação', 'Projeto de alfabetização', self.assuntos)
        self.assertEqual(resultado.assunto_id, 2)

    def test_tie_is_not_arbitrary(self):
        ambíguos = [
            assunto(1, 'A', 'saúde'),
            assunto(2, 'B', 'saúde'),
        ]
        resultado = classificar('saúde', '', ambíguos)
        self.assertIsNone(resultado.assunto_id)
        self.assertTrue(resultado.ambigua)

    def test_exclusion_wins(self):
        resultado = classificar(
            'saúde mental descontinuado', '',
            [assunto(1, 'A', 'saúde mental', exclusao='descontinuado')],
        )
        self.assertIsNone(resultado.assunto_id)
        self.assertTrue(resultado.excluido)

    def test_exclusion_applies_only_to_the_declaring_subject(self):
        assuntos = [
            assunto(1, 'A', 'futebol', exclusao='futebol'),
            assunto(2, 'B', 'alfabetização'),
        ]
        resultado = classificar('futebol e alfabetização', '', assuntos)
        self.assertEqual(resultado.assunto_id, 2)
        self.assertFalse(resultado.excluido)
        self.assertEqual(resultado.detalhes['A'], 0)
        self.assertEqual(resultado.detalhes['B'], 1)

    def test_all_candidates_excluded(self):
        assuntos = [
            assunto(1, 'A', 'futebol', exclusao='futebol'),
            assunto(2, 'B', 'alfabetização', exclusao='alfabetização'),
        ]
        resultado = classificar('futebol e alfabetização', '', assuntos)
        self.assertIsNone(resultado.assunto_id)
        self.assertTrue(resultado.excluido)

    def test_no_match(self):
        resultado = classificar('esporte', '', self.assuntos)
        self.assertIsNone(resultado.assunto_id)
        self.assertTrue(resultado.sem_correspondencia)

    def test_empty_text(self):
        resultado = classificar('', '', self.assuntos)
        self.assertTrue(resultado.sem_correspondencia)

    def test_higher_score_wins(self):
        resultado = classificar(
            'alfabetização e aprendizagem', '',
            [assunto(1, 'A', 'alfabetização'), assunto(2, 'B', 'alfabetização\naprendizagem')],
        )
        self.assertEqual(resultado.assunto_id, 2)
        self.assertEqual(resultado.pontuacao, 2)

    def test_word_boundary_avoid_substring(self):
        resultado = classificar('desaprendizagem', '', [assunto(1, 'A', 'aprendizagem')])
        self.assertTrue(resultado.sem_correspondencia)


class CenariosReaisTest(unittest.TestCase):
    """Regression cases using the terms registered in the live Odoo instance."""

    def setUp(self):
        self.assuntos = [
            assunto(1, 'NR-1 e riscos psicossociais',
                   'NR-1\nrisco psicossocial\nriscos psicossociais\nassédio moral\nburnout\nsofrimento psíquico',
                   area='Corporativo'),
            assunto(2, 'Avaliação de riscos organizacionais',
                   'avaliação de riscos organizacionais\ngestão de riscos organizacionais\n'
                   'riscos ocupacionais\ninvestigação de acidente de trabalho', area='Corporativo'),
            assunto(3, 'Aprendizagem e alfabetização',
                   'aprendizagem\nalfabetização\nletramento', area='Educação'),
            assunto(4, 'Avaliação educacional',
                   'avaliação educacional\navaliação da aprendizagem\nIDEB\nrendimento escolar',
                   area='Educação'),
            assunto(5, 'Saúde mental e cognição',
                   'saúde mental\ncognição\nneurociência\ndemência\ndepressão\nansiedade', area='Saúde'),
            assunto(6, 'Avaliação neurocognitiva',
                   'avaliação neurocognitiva\ntestes neuropsicológicos\ntriagem cognitiva', area='Saúde'),
            assunto(7, 'Políticas públicas de saúde e educação',
                   'política pública\npolíticas públicas\nregulamentação', area='Transversal'),
        ]

    def test_positive_case_per_topic(self):
        casos = [
            ('Nova NR-1 exige gestão de riscos psicossociais nas empresas',
             'Empresas precisam prevenir assédio moral e burnout.', 1),
            ('Ministério lança política nacional de saúde mental',
             'Ação previne depressão e ansiedade no trabalho.', 5),
            ('MEC divulga resultado do IDEB 2024', 'Índice mede o rendimento escolar.', 4),
            ('Estudo avalia aprendizagem de crianças', 'Pesquisa acompanha alfabetização e letramento.', 3),
            ('Pesquisa em neurociência investiga cognição e demência', '', 5),
            ('Testes neuropsicológicos para triagem cognitiva',
             'Avaliação neurocognitiva em idosos.', 6),
            ('Governo discute políticas públicas de educação e saúde', 'Nova regulamentação.', 7),
        ]
        for titulo, resumo, esperado in casos:
            resultado = classificar(titulo, resumo, self.assuntos)
            self.assertEqual(resultado.assunto_id, esperado, titulo)

    def test_unrelated_case(self):
        for titulo, resumo in (
            ('Câmara aprova orçamento anual', 'Emendas parlamentares.'),
            ('Seleção brasileira vence amistoso', 'Gols no segundo tempo.'),
            ('Avaliação de desempenho dos servidores', 'Relatório de resultados.'),
        ):
            resultado = classificar(titulo, resumo, self.assuntos)
            self.assertIsNone(resultado.assunto_id, titulo)
            self.assertTrue(resultado.sem_correspondencia, titulo)

    def test_ambiguous_case_is_not_classified(self):
        resultado = classificar(
            'Política pública de saúde mental nas escolas',
            'Programa promove saúde mental e aprendizagem.', self.assuntos,
        )
        self.assertIsNone(resultado.assunto_id)
        self.assertTrue(resultado.ambigua)


if __name__ == '__main__':
    unittest.main()
