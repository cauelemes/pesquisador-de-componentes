"""Testes da conferência determinística: "a LLM julga, o código confere" (sem rede, sem créditos).

    python -m unittest tests.test_conferencia -v

Cada teste parte de um achado VÁLIDO (que passa em todas as regras) e estraga uma coisa só.
"""

import unittest

from backend.compartilhado.modelos import AchadoComponente, PaginaLida, PedidoComponente
from backend.pesquisa.conferencia import conferir_achado, conferir_achados, normalizar_texto

URL = "https://acme.example/ac1234"

# Um trecho de "datasheet" de mentira. Repare nos detalhes tipográficos de propósito:
# espaço não separável ( ) em "4.5 V", meia-risca (–) no intervalo, acento em "Tensão", micro (µ).
PAGINA = (
    "AC1234 — Regulador buck síncrono de 3 A.\n"
    "Tensão de entrada: 4.5 V – 28 V.\n"
    "Corrente de saída máxima: 3 A.\n"
    "Corrente quiescente: 85 µA (típico).\n"
    "Temperatura de operação: -40 °C a 125 °C."
)

PEDIDO = PedidoComponente(
    categoria="regulador buck",
    requisitos=[
        {"parametro": "tensão de entrada máxima", "operador": ">=", "valor": 24, "unidade": "V"},
        {"parametro": "corrente de saída", "operador": ">=", "valor": 2, "unidade": "A"},
        {"parametro": "corrente quiescente", "operador": "<=", "valor": 100, "unidade": "µA"},
    ],
)


def evidencia(requisito="tensão de entrada máxima", citacao="Tensão de entrada: 4.5 V – 28 V.", **mudancas):
    return {"requisito": requisito, "situacao": "confirmado", "citacao": citacao} | mudancas


def achado(**mudancas) -> AchadoComponente:
    dados = {
        "fabricante": "Acme",
        "mpn": "AC1234",
        "url": URL,
        "ciclo_de_vida": "ativo",
        "evidencias": [evidencia()],
    } | mudancas
    return AchadoComponente(**dados)


def paginas(conteudo=PAGINA, url=URL, erro=None) -> dict[str, PaginaLida]:
    return {url: PaginaLida(url=url, conteudo=conteudo, erro=erro)}


def conferir(a: AchadoComponente, lidas=None):
    return conferir_achado(a, paginas() if lidas is None else lidas, PEDIDO)


class TestNormalizarTexto(unittest.TestCase):
    def test_unifica_variacoes(self):
        variantes = ["Tensão – 3,3 V", "tensao - 3,3 v", "TENSÃO —  3,3   V", "Tensão − 3,3 V"]
        self.assertEqual({normalizar_texto(v) for v in variantes}, {"tensao - 3,3 v"})

    def test_micro_e_mu(self):
        self.assertEqual(normalizar_texto("85 µA"), normalizar_texto("85 μA"))


class TestAchadoValido(unittest.TestCase):
    def test_achado_correto_passa_sem_alertas(self):
        r = conferir(achado())
        self.assertEqual((r.situacao, r.motivo, r.alertas), ("valido", "", []))
        self.assertEqual(r.achado.evidencias[0].situacao, "confirmado")

    def test_original_nao_e_alterado(self):
        original = achado(evidencias=[evidencia(citacao="frase inventada 30 V")])
        conferir(original)
        self.assertEqual(original.evidencias[0].situacao, "confirmado")  # o resultado é uma CÓPIA


class TestRegra1Citacao(unittest.TestCase):
    def test_citacao_inventada_vira_indicio(self):
        r = conferir(achado(evidencias=[evidencia(citacao="Tensão de entrada: até 40 V.")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "indicio")
        self.assertEqual(r.situacao, "valido")  # evidência fraca não recusa o achado; só avisa
        self.assertEqual(len(r.alertas), 1)
        self.assertIn("citação não encontrada", r.alertas[0])

    def test_hifen_espaco_acento_e_maiuscula_diferentes_ainda_aceitos(self):
        # Na página: espaço não separável, meia-risca e acento. Na citação: espaço comum, hífen, sem acento e MAIÚSCULAS.
        r = conferir(achado(evidencias=[evidencia(citacao="TENSAO DE ENTRADA: 4.5 V  -  28 V")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "confirmado")
        self.assertEqual(r.alertas, [])

    def test_micro_digitado_como_mu_grego_ainda_aceito(self):
        citacao = "Corrente quiescente: 85 μA"  # página usa U+00B5; aqui, U+03BC
        r = conferir(achado(evidencias=[evidencia("corrente quiescente", citacao)]))
        self.assertEqual(r.achado.evidencias[0].situacao, "confirmado")

    def test_confirmado_sem_citacao_vira_indicio(self):
        r = conferir(achado(evidencias=[evidencia(citacao="")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "indicio")
        self.assertIn("sem citação", r.alertas[0])

    def test_contradiz_com_citacao_inventada_so_avisa(self):
        r = conferir(achado(evidencias=[evidencia(citacao="máximo de 12 V", situacao="contradiz")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "contradiz")
        self.assertEqual(len(r.alertas), 1)

    def test_nao_encontrado_sem_citacao_nao_gera_alerta(self):
        r = conferir(achado(evidencias=[evidencia(citacao="", situacao="nao_encontrado")]))
        self.assertEqual(r.alertas, [])


class TestRegra2Mpn(unittest.TestCase):
    def test_mpn_ausente_recusa(self):
        r = conferir(achado(mpn="XY9999"))
        self.assertEqual(r.situacao, "recusado")
        self.assertIn("MPN não aparece", r.motivo)
        self.assertTrue(any("MPN não aparece" in a for a in r.alertas))

    def test_mpn_com_caixa_e_tipo_de_hifen_diferentes_e_aceito(self):
        pagina = paginas(PAGINA.replace("AC1234", "AC‑1234"))  # página: "AC‑1234" com hífen não separável
        evs = [evidencia(citacao="")]  # sem citação, para o teste olhar só o MPN
        self.assertEqual(conferir(achado(mpn="ac-1234", evidencias=evs), pagina).situacao, "valido")

    def test_mpn_sem_o_hifen_da_pagina_nao_casa(self):
        # Unificamos o TIPO de hífen, mas não o removemos: "AC1234" e "AC-1234" podem ser peças diferentes.
        pagina = paginas(PAGINA.replace("AC1234", "AC-1234"))
        self.assertEqual(conferir(achado(mpn="AC1234"), pagina).situacao, "recusado")


class TestRegra3Numeros(unittest.TestCase):
    def test_ponto_decimal_em_ingles(self):
        pagina = "AC1234 datasheet. Input voltage range: 4.5 V to 28 V. Quiescent current: 0.085 mA."
        evs = [
            evidencia(citacao="Input voltage range: 4.5 V to 28 V"),
            evidencia("corrente quiescente", "Quiescent current: 0.085 mA"),  # 85 µA <= 100 µA
        ]
        r = conferir(achado(evidencias=evs), paginas(pagina))
        self.assertEqual([e.situacao for e in r.achado.evidencias], ["confirmado", "confirmado"])
        self.assertEqual(r.alertas, [])

    def test_valor_que_nao_atende_vira_contradiz(self):
        pagina = "AC1234. Input voltage: 4.5 V to 18 V."  # 18 V < 24 V exigidos
        r = conferir(achado(evidencias=[evidencia(citacao="Input voltage: 4.5 V to 18 V")]), paginas(pagina))
        self.assertEqual(r.achado.evidencias[0].situacao, "contradiz")
        self.assertIn("nenhum valor da citação atende", r.alertas[0])

    def test_unidade_errada_vira_indicio(self):
        # A frase existe na página, mas só fala de corrente (A); o requisito é de tensão (V).
        r = conferir(achado(evidencias=[evidencia(citacao="Corrente de saída máxima: 3 A.")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "indicio")
        self.assertIn("sem quantidade em V", r.alertas[0])

    def test_citacao_sem_numero_vira_indicio(self):
        r = conferir(achado(evidencias=[evidencia(citacao="Regulador buck síncrono")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "indicio")

    def test_codigo_da_peca_na_citacao_nao_vira_quantidade(self):
        # "AC1234" não pode virar "1234 ..." e a citação só tem "3 A" como quantidade.
        r = conferir(achado(evidencias=[evidencia("corrente de saída", "AC1234 — Regulador buck síncrono de 3 A.")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "confirmado")

    def test_requisito_fora_do_pedido_vira_indicio(self):
        r = conferir(achado(evidencias=[evidencia(requisito="frequência de chaveamento")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "indicio")
        self.assertIn("não existe no pedido", r.alertas[0])

    def test_nome_do_requisito_tolerante_a_acento_e_caixa(self):
        r = conferir(achado(evidencias=[evidencia(requisito="Tensao de Entrada Maxima")]))
        self.assertEqual(r.achado.evidencias[0].situacao, "confirmado")


class TestRegra4Url(unittest.TestCase):
    def test_url_nao_lida_recusa(self):
        r = conferir(achado(url="https://outro.example/ac1234"))
        self.assertEqual(r.situacao, "recusado")
        self.assertIn("URL não está entre as páginas lidas", r.motivo)
        self.assertEqual(r.achado.evidencias[0].situacao, "indicio")  # sem página, nada fica confirmado

    def test_pagina_com_erro_nao_conta_como_lida(self):
        r = conferir(achado(), paginas(conteudo="", erro="timeout"))
        self.assertEqual(r.situacao, "recusado")

    def test_url_com_diferenca_de_caixa_e_barra_final_e_aceita(self):
        r = conferir(achado(url="https://ACME.example/ac1234/"))
        self.assertEqual(r.situacao, "valido")


class TestRegra5CicloDeVida(unittest.TestCase):
    def test_obsoleto_recusa(self):
        r = conferir(achado(ciclo_de_vida="obsoleto"))
        self.assertEqual(r.situacao, "recusado")
        self.assertIn("obsoleto", r.motivo)
        self.assertTrue(any("obsoleto" in a for a in r.alertas))

    def test_nrnd_e_desconhecido_tambem_recusam(self):
        for ciclo in ("nrnd", "desconhecido"):
            with self.subTest(ciclo=ciclo):
                self.assertEqual(conferir(achado(ciclo_de_vida=ciclo)).situacao, "recusado")

    def test_varios_motivos_juntos(self):
        r = conferir(achado(mpn="XY9999", ciclo_de_vida="obsoleto"))
        self.assertEqual(r.motivo, "MPN não aparece na página; ciclo de vida: obsoleto")


class TestConferirAchados(unittest.TestCase):
    def test_junta_resultados_e_alertas(self):
        r = conferir_achados([achado(), achado(ciclo_de_vida="obsoleto")], paginas(), PEDIDO)
        self.assertEqual([c.situacao for c in r.achados], ["valido", "recusado"])
        self.assertEqual(r.alertas, ["AC1234: ciclo de vida: obsoleto"])

    def test_alerta_corta_texto_longo_da_llm(self):
        r = conferir_achados([achado(mpn="X" * 60)], paginas(), PEDIDO)
        self.assertLessEqual(len(r.alertas[0].split(":")[0]), 40)


if __name__ == "__main__":
    unittest.main()
