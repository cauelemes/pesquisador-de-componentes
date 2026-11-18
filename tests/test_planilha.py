"""Testes da planilha .xlsx (sem rede, sem créditos).

    python -m unittest tests.test_planilha -v
"""

import unittest
from io import BytesIO

from openpyxl import load_workbook

from backend.compartilhado.modelos import ROTULO_RESUMO_DA_LLM, AchadoComponente, AchadoRecusado, PedidoComponente
from backend.planilhas import gera_planilha as gp

PEDIDO = PedidoComponente(
    categoria="regulador linear (LDO)",
    requisitos=[
        {"parametro": "tensão de saída", "operador": "=", "valor": 3.3, "unidade": "V"},
        {"parametro": "corrente de saída", "operador": ">=", "valor": 1, "unidade": "A"},
    ],
    encapsulamento="SOT-223",
)


def achado(mpn="AC1117-3.3", fabricante="Acme", **mudancas) -> AchadoComponente:
    evidencias = [
        {"requisito": "tensão de saída", "situacao": "confirmado", "citacao": "Output voltage: 3.3 V", "valor": 3.3,
         "unidade": "V", "justificativa": "valor fixo"},
        {"requisito": "corrente de saída", "situacao": "indicio", "citacao": "up to 1 A"},
        {"requisito": "encapsulamento", "situacao": "confirmado", "citacao": "Package: SOT-223"},
    ]
    dados = {"fabricante": fabricante, "mpn": mpn, "url": "https://acme.example/ac1117", "ciclo_de_vida": "ativo",
             "evidencias": evidencias} | mudancas
    return AchadoComponente(**dados)


def abrir(conteudo: bytes):
    return load_workbook(BytesIO(conteudo))


def linhas(aba) -> list[list]:
    return [[c.value for c in linha] for linha in aba.iter_rows()]


class TestEstrutura(unittest.TestCase):
    def setUp(self):
        self.livro = abrir(gp.gerar_planilha(PEDIDO, [achado(), achado("BT33", "Beta")], "Dois LDOs.", ["alerta 1"]))

    def test_tres_abas(self):
        self.assertEqual(self.livro.sheetnames, ["Alternativas", "Evidências", "Pedido e alertas"])

    def test_alternativas(self):
        aba = self.livro["Alternativas"]
        cabecalho, primeira = linhas(aba)[:2]
        self.assertEqual(cabecalho, ["Nº", "Fabricante", "MPN", "Ciclo de vida", "URL da fonte",
                                     "tensão de saída (= 3,3 V)", "corrente de saída (≥ 1 A)",
                                     "Encapsulamento (SOT-223)"])
        self.assertEqual(primeira, [1, "Acme", "AC1117-3.3", "ativo", "https://acme.example/ac1117",
                                    "3,3 V (confirmado)", "(indício)", "SOT-223 (confirmado)"])
        self.assertEqual(aba.max_row, 3)  # cabeçalho + 2 componentes

    def test_encapsulamento_so_aparece_quando_foi_achado(self):
        casos = {
            "confirmado": "SOT-223 (confirmado)",
            "indicio": "SOT-223 (indício)",
            "nao_encontrado": "(não encontrado)",  # o pedido NÃO é escrito como se tivesse sido achado
            "contradiz": "(contradiz)",
        }
        for situacao, esperado in casos.items():
            with self.subTest(situacao=situacao):
                citacao = "" if situacao == "nao_encontrado" else "Package: SOT-89"
                evidencia = {"requisito": "encapsulamento", "situacao": situacao, "citacao": citacao}
                livro = abrir(gp.gerar_planilha(PEDIDO, [achado(evidencias=[evidencia])], "", []))
                self.assertEqual(livro["Alternativas"]["H2"].value, esperado)
        sem = abrir(gp.gerar_planilha(PEDIDO, [achado(evidencias=[])], "", []))
        self.assertEqual(sem["Alternativas"]["H2"].value, "sem evidência")

    def test_operadores_padronizados_e_sem_formula(self):
        requisitos = [
            {"parametro": "tensão", "operador": "=", "valor": 3.3, "unidade": "V"},
            {"parametro": "corrente", "operador": ">=", "valor": 1, "unidade": "A"},
            {"parametro": "queda", "operador": "<=", "valor": 1.2, "unidade": "V"},
            {"parametro": "entrada", "operador": "faixa", "valor": 4.5, "valor_max": 15, "unidade": "V"},
        ]
        pedido = PedidoComponente(categoria="LDO", requisitos=requisitos)
        livro = abrir(gp.gerar_planilha(pedido, [achado(evidencias=[])], "", []))
        operadores = [linha[1] for linha in linhas(livro["Pedido e alertas"]) if linha[0] in ("tensão", "corrente",
                                                                                              "queda", "entrada")]
        self.assertEqual(operadores, ["igual (=)", "maior ou igual (≥)", "menor ou igual (≤)", "faixa"])
        self.assertEqual(linhas(livro["Alternativas"])[0][5:9],
                         ["tensão (= 3,3 V)", "corrente (≥ 1 A)", "queda (≤ 1,2 V)", "entrada (faixa 4,5 a 15 V)"])
        for aba in livro.worksheets:  # nenhuma célula de nenhuma aba vira fórmula
            for linha in aba.iter_rows():
                for celula in linha:
                    self.assertNotEqual(celula.data_type, "f", f"{aba.title}!{celula.coordinate}")
                    if isinstance(celula.value, str):
                        self.assertFalse(celula.value.startswith(gp._INICIO_DE_FORMULA), celula.value)

    def test_cores_por_situacao(self):
        aba = self.livro["Alternativas"]
        self.assertEqual(aba["F2"].fill.fgColor.rgb[-6:], gp.CORES_SITUACAO["confirmado"])
        self.assertEqual(aba["G2"].fill.fgColor.rgb[-6:], gp.CORES_SITUACAO["indicio"])

    def test_evidencias(self):
        aba = self.livro["Evidências"]
        todas = linhas(aba)
        self.assertEqual(todas[0], ["MPN", "Requisito", "Situação", "Valor", "Unidade", "Citação literal",
                                    "Justificativa", "URL"])
        self.assertEqual(len(todas), 1 + 2 * 3)  # 3 evidências por componente
        self.assertEqual(todas[1][:6], ["AC1117-3.3", "tensão de saída", "confirmado", 3.3, "V", "Output voltage: 3.3 V"])
        self.assertIsInstance(todas[1][3], float)  # o valor continua NÚMERO na célula

    def test_pedido_e_alertas(self):
        valores = [v for linha in linhas(self.livro["Pedido e alertas"]) for v in linha if v is not None]
        for esperado in ("regulador linear (LDO)", "SOT-223", "tensão de saída", "igual (=)", "maior ou igual (≥)",
                         "Dois LDOs.",
                         "alerta 1", "(nenhum)", "(sem comentário)"):  # sem recusados nem comentário da LLM
            self.assertIn(esperado, valores)

    def test_resumo_conferido_recusados_e_comentario_da_llm_nos_lugares_certos(self):
        recusado = AchadoRecusado(fabricante="Diodes", mpn="AP2114H-3.3TRG1", motivo="MPN não aparece na página")
        livro = abrir(gp.gerar_planilha(PEDIDO, [achado()], "A LLM propôs 2 componentes; 1 passou.", ["alerta"],
                                        recusados=[recusado], resumo_da_llm="Encontrei 2 LDOs ótimos."))
        pares = [(linha[0], linha[1]) for linha in linhas(livro["Pedido e alertas"]) if linha[0] or linha[1]]
        rotulos = [rotulo for rotulo, _ in pares]
        # Ordem: o conferido primeiro, depois os recusados, depois o comentário da LLM (rotulado), depois os alertas.
        i_conferido, i_recusados = rotulos.index("Resumo conferido"), rotulos.index("Recusados na conferência")
        i_llm, i_alertas = rotulos.index(ROTULO_RESUMO_DA_LLM), rotulos.index("Alertas")
        self.assertLess(i_conferido, i_recusados)
        self.assertLess(i_recusados, i_llm)
        self.assertLess(i_llm, i_alertas)
        self.assertEqual(pares[i_conferido][1], "A LLM propôs 2 componentes; 1 passou.")
        self.assertEqual(pares[i_recusados + 1], ("AP2114H-3.3TRG1 (Diodes)", "MPN não aparece na página"))
        self.assertEqual(pares[i_llm], ("Comentário da LLM (não conferido)", "Encontrei 2 LDOs ótimos."))

    def test_planilha_da_demonstracao_avisa(self):
        normal = abrir(gp.gerar_planilha(PEDIDO, [achado()], "ok", []))
        demonstracao = abrir(gp.gerar_planilha(PEDIDO, [achado()], "ok", [], demonstracao=True))
        self.assertEqual(linhas(demonstracao["Pedido e alertas"])[1][:2], ["Atenção", gp.AVISO_DEMONSTRACAO])
        self.assertIn("Gerado a partir de uma execução gravada", gp.AVISO_DEMONSTRACAO)
        self.assertNotIn(gp.AVISO_DEMONSTRACAO, [v for linha in linhas(normal["Pedido e alertas"]) for v in linha])

    def test_formatacao(self):
        for aba in self.livro.worksheets:
            with self.subTest(aba=aba.title):
                self.assertEqual(aba.freeze_panes, "A2")
                self.assertTrue(aba["A1"].font.bold)
        self.assertTrue(self.livro["Evidências"]["F2"].alignment.wrap_text)  # citação: coluna longa

    def test_url_e_texto_sem_hiperlink(self):
        self.assertIsNone(self.livro["Alternativas"]["E2"].hyperlink)

    def test_numero_de_achados(self):
        for n in (0, 6):
            with self.subTest(n=n), self.assertRaises(gp.ErroDePlanilha):
                gp.gerar_planilha(PEDIDO, [achado()] * n, "", [])


class TestSeguranca(unittest.TestCase):
    MALICIOSOS = ["=HYPERLINK(\"http://mal.example\",\"clique\")", "+1+1", "-2+3", "@SUM(A1)", "\t=1", "\r=1"]

    def test_inicio_de_formula_neutralizado_em_todas_as_abas(self):
        ataque = self.MALICIOSOS
        pedido = PEDIDO.model_copy(update={"categoria": ataque[0], "encapsulamento": ataque[3]})
        evil = achado(mpn=ataque[1], fabricante=ataque[0], evidencias=[
            {"requisito": ataque[2], "situacao": "confirmado", "citacao": ataque[3], "justificativa": ataque[0],
             "unidade": ataque[1]},
        ])
        livro = abrir(gp.gerar_planilha(pedido, [evil], ataque[0], ataque))
        celulas = [c for aba in livro.worksheets for linha in aba.iter_rows() for c in linha]
        self.assertTrue(any(c.value == "'" + ataque[0] for c in celulas))  # o texto está lá, neutralizado
        for celula in celulas:
            with self.subTest(celula=celula.coordinate, aba=celula.parent.title):
                self.assertNotEqual(celula.data_type, "f")  # nenhuma fórmula em lugar nenhum
                if isinstance(celula.value, str):
                    self.assertFalse(celula.value.startswith(gp._INICIO_DE_FORMULA), celula.value)

    def test_texto_seguro(self):
        for texto in self.MALICIOSOS:
            with self.subTest(texto=texto):
                self.assertEqual(gp._texto_seguro(texto), "'" + texto)
        self.assertEqual(gp._texto_seguro("Output 3.3 V"), "Output 3.3 V")  # texto comum não muda
        self.assertEqual(gp._texto_seguro(None), "")

    def test_caracteres_de_controle_removidos(self):
        sujo = achado(evidencias=[{"requisito": "x", "situacao": "confirmado", "citacao": "a\x00b\x07c\x1bd"}])
        livro = abrir(gp.gerar_planilha(PEDIDO, [sujo], "", []))  # sem a limpeza, o openpyxl levantaria erro aqui
        self.assertEqual(livro["Evidências"]["F2"].value, "abcd")

    def test_texto_enorme_e_cortado(self):
        self.assertEqual(len(gp._texto_seguro("x" * 100_000)), gp.MAX_CARACTERES_CELULA)
        self.assertEqual(len(gp._texto_seguro("=" + "x" * 100_000)), gp.MAX_CARACTERES_CELULA)  # mesmo com o "'"
        # `model_copy` não revalida: simula um texto que escapou dos limites do modelo.
        enorme = achado()
        enorme.evidencias[0] = enorme.evidencias[0].model_copy(update={"justificativa": "y" * 100_000})
        livro = abrir(gp.gerar_planilha(PEDIDO, [enorme], "z" * 100_000, []))
        self.assertEqual(len(livro["Evidências"]["G2"].value), gp.MAX_CARACTERES_CELULA)


if __name__ == "__main__":
    unittest.main()
