"""Testes da conferência determinística: "a LLM julga, o código confere" (sem rede, sem créditos).

    python -m unittest tests.test_conferencia -v

Cada teste parte de um achado VÁLIDO (que passa em todas as regras) e estraga uma coisa só.
"""

import unittest

from pydantic import ValidationError

from backend.compartilhado.modelos import AchadoComponente, PaginaLida, PedidoComponente
from backend.pesquisa import conferencia
from backend.pesquisa.conferencia import (
    citacao_confere,
    conferir_achado,
    conferir_achados,
    normalizar_texto,
    raiz_do_mpn,
)

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

    def test_confirmado_sem_citacao_nem_chega_aqui(self):
        # Quem barra agora é o modelo Pydantic (ver tests/test_modelos.py): o achado nem pode ser criado.
        with self.assertRaises(ValidationError):
            achado(evidencias=[evidencia(citacao="")])

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
        evs = [evidencia(citacao="", situacao="nao_encontrado")]  # sem citação, para o teste olhar só o MPN
        self.assertEqual(conferir(achado(mpn="ac-1234", evidencias=evs), pagina).situacao, "valido")

    def test_mpn_sem_o_hifen_da_pagina_nao_casa(self):
        # Unificamos o TIPO de hífen, mas não o removemos: "AC1234" e "AC-1234" podem ser peças diferentes.
        pagina = paginas(PAGINA.replace("AC1234", "AC-1234"))
        self.assertEqual(conferir(achado(mpn="AC1234"), pagina).situacao, "recusado")


# Um trecho de "PDF" com os defeitos reais: hífen de fim de linha, espaço faltando e trechos distantes.
PDF = (
    "AZ1117 1A LOW DROPOUT LINEAR REGULATOR. Features: Three-terminal Adjustable or Fixed Voltages. "
    "Output current up to1A. Low Dropout Voltage: 1.15V at 1A Output Cur-\nrent. Operating range -40 °C to 125 °C. "
    "Ordering information: 1.5V, 1.8V, 2.5V, 3.3V and 5.0V. Package: SOT-223-4, TO-252-2."
)
REQ_SAIDA = {"parametro": "tensão de saída", "operador": "=", "valor": 3.3, "unidade": "V"}
PEDIDO_SOT223 = PedidoComponente(categoria="LDO", requisitos=[REQ_SAIDA], encapsulamento="SOT-223")


def conferir_pdf(citacao, requisito="tensão de saída", pedido=PEDIDO_SOT223, situacao="confirmado"):
    """Confere UMA evidência contra a página PDF; devolve (situação final, alertas)."""
    a = achado(mpn="AZ1117", ciclo_de_vida="ativo", evidencias=[evidencia(requisito, citacao, situacao=situacao)])
    r = conferir_achado(a, paginas(PDF), pedido)
    return r.achado.evidencias[0].situacao, r.alertas


class TestCitacaoEmFragmentos(unittest.TestCase):
    def textos(self, pagina=PDF):
        """A página nas duas formas que a conferência usa (normalizada, e a de reserva)."""
        texto = normalizar_texto(pagina)
        return texto, conferencia._alfanumerico(texto)

    def test_reticencias_aceitas(self):
        citacao = "Three-terminal Adjustable or Fixed Voltages ... 1.5V, 1.8V, 2.5V, 3.3V and 5.0V"
        self.assertEqual(citacao_confere(citacao, *self.textos()), "ok")
        self.assertEqual(conferir_pdf(citacao), ("confirmado", []))  # e os números (3.3V) ainda são conferidos

    def test_reticencias_unicode(self):
        citacao = "Three-terminal Adjustable or Fixed Voltages \u2026 1.5V, 1.8V, 2.5V, 3.3V and 5.0V"
        self.assertEqual(citacao_confere(citacao, *self.textos()), "ok")

    def test_reticencias_coladas_ao_numero_nao_escondem_o_numero(self):
        citacao = "Three-terminal Adjustable or Fixed Voltages...3.3V and 5.0V"
        self.assertEqual(conferir_pdf(citacao)[0], "confirmado")

    def test_um_fragmento_inventado_rejeita_a_citacao_inteira(self):
        citacao = "Three-terminal Adjustable or Fixed Voltages ... Output voltage fixed at 3.3V"
        self.assertEqual(citacao_confere(citacao, *self.textos()), "ausente")
        situacao, alertas = conferir_pdf(citacao)
        self.assertEqual(situacao, "indicio")
        self.assertIn("AZ1117: tensão de saída: citação não encontrada na página", alertas)

    def test_hifen_de_fim_de_linha_aceito(self):
        # A página tem "Cur-\nrent"; a LLM pode escrever "Current" ou copiar "Cur-rent". Os dois casam.
        for citacao in ("Low Dropout Voltage: 1.15V at 1A Output Current", "Low Dropout Voltage: 1.15V at 1A Output Cur-rent"):
            with self.subTest(citacao=citacao):
                self.assertEqual(citacao_confere(citacao, *self.textos()), "ok")

    def test_espacamento_estranho_aceito(self):
        self.assertEqual(citacao_confere("Output current up to 1A", *self.textos()), "ok")  # página: "up to1A"

    def test_forma_de_reserva_mantem_o_decimal(self):
        casos = {
            "3,3 V": "3.3v", "3.3V": "3.3v", "33 V": "33v", "1.15V at 1A": "1.15vat1a",
            "Cur-\nrent": "current", "SOT-223-4": "sot2234", "fim. 3 V": "fim3v", "3.3.": "3.3",
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(conferencia._alfanumerico(texto), esperado)

    def test_valor_decimal_errado_nao_casa(self):
        casos = [
            ("Output voltage: 3.3 V fixed", "Output voltage: 33 V fixed."),  # citação 3.3 V, página só 33 V
            ("Output current: 1.5 A maximum", "Output current: 15 A maximum."),
            ("Output current: 15 A maximum", "Output current: 1.5 A maximum."),  # e o contrário
        ]
        for citacao, pagina in casos:
            with self.subTest(citacao=citacao):
                self.assertEqual(citacao_confere(citacao, *self.textos(pagina)), "ausente")

    def test_nao_casa_no_meio_de_outro_numero(self):
        casos = [
            ("3.3 V output current", "Fixed 13.3 V output current."),  # começo colado a dígito
            ("1.5 A maximum output", "Up to 11.5 A maximum output."),
            ("output current is 1.5", "The output current is 1.55 A."),  # fim colado a dígito
            ("3.3 V output current", "Rev 2.3.3 V output current"),  # antes: separador colado a dígito
            ("output voltage is 3", "The output voltage is 3,3 V."),  # depois: separador seguido de dígito
        ]
        for citacao, pagina in casos:
            with self.subTest(citacao=citacao, pagina=pagina):
                self.assertEqual(citacao_confere(citacao, *self.textos(pagina)), "ausente")

    def test_borda_valida_no_comeco_e_no_fim_da_pagina(self):
        self.assertEqual(citacao_confere("3.3 V output current", *self.textos("3.3 V output current")), "ok")
        self.assertEqual(citacao_confere("output current is 1.5", *self.textos("Output current is 1.5")), "ok")
        self.assertEqual(citacao_confere("3.3 V output current", *self.textos("(3.3 V output current)")), "ok")
        self.assertEqual(citacao_confere("output current is 1.5", *self.textos("output current is 1.5. Next")), "ok")

    def test_uma_ocorrencia_valida_entre_duas_invalidas_basta(self):
        pagina = "Fixed 13.3 V output current; fixed 3.3 V output current; fixed 23.3 V output current."
        self.assertEqual(citacao_confere("3.3 V output current", *self.textos(pagina)), "ok")

    def test_borda_tambem_vale_na_forma_de_reserva(self):
        # "Cur-\nrent" obriga a usar a forma de reserva; mesmo nela, "3.3 V" não pode casar dentro de "13.3 V".
        self.assertEqual(citacao_confere("3.3 V Output Current", *self.textos("13.3 V Output Cur-\nrent")), "ausente")
        self.assertEqual(citacao_confere("3.3 V Output Current", *self.textos("Fixed 3.3 V Output Cur-\nrent")), "ok")

    def test_virgula_decimal_casa_com_ponto(self):
        self.assertEqual(citacao_confere("Output voltage: 3,3 V fixed", *self.textos("Output voltage: 3.3 V fixed.")), "ok")
        self.assertEqual(citacao_confere("Output voltage: 3.3V fixed", *self.textos("Output voltage: 3,3 V fixed.")), "ok")

    def test_valor_errado_rebaixa_a_evidencia(self):
        pagina = "AC1234 regulator. Output voltage: 33 V fixed."
        a = achado(mpn="AC1234", evidencias=[evidencia("tensão de saída", "Output voltage: 3.3 V fixed")])
        r = conferir_achado(a, paginas(pagina), PEDIDO_SOT223)
        self.assertEqual(r.achado.evidencias[0].situacao, "indicio")
        self.assertIn("AC1234: tensão de saída: citação não encontrada na página", r.alertas)

    def test_fragmentos_curtos_demais_nao_bastam_sozinhos(self):
        citacao = "3.3V ... 1A ... SOT-223"  # tudo isso está na página, mas cada pedaço tem menos de 12 caracteres
        self.assertEqual(citacao_confere(citacao, *self.textos()), "curta")
        situacao, alertas = conferir_pdf(citacao)
        self.assertEqual(situacao, "indicio")
        self.assertIn("AZ1117: tensão de saída: citação curta demais", alertas)

    def test_fragmento_curto_e_ignorado_quando_ha_um_longo(self):
        # "inventado" tem só 9 caracteres: é descartado, e o fragmento longo (real) decide.
        citacao = "Ordering information: 1.5V, 1.8V, 2.5V, 3.3V ... inventado"
        self.assertEqual(citacao_confere(citacao, *self.textos()), "ok")


class TestEncapsulamentoECicloDeVida(unittest.TestCase):
    def test_encapsulamento_casa_com_variantes(self):
        for citacao in ("Package: SOT-223-4, TO-252-2.", "Package: SOT-223-4"):
            with self.subTest(citacao=citacao):
                self.assertEqual(conferir_pdf(citacao, requisito="encapsulamento"), ("confirmado", []))

    def test_encapsulamento_sem_hifen_no_pedido(self):
        pedido = PEDIDO_SOT223.model_copy(update={"encapsulamento": "SOT223"})
        self.assertEqual(conferir_pdf("Package: SOT-223-4", "encapsulamento", pedido)[0], "confirmado")

    def test_citacao_sem_o_encapsulamento_pedido_vira_indicio(self):
        situacao, alertas = conferir_pdf("Package: SOT-223-4, TO-252-2.", "encapsulamento",
                                         PEDIDO_SOT223.model_copy(update={"encapsulamento": "SOT-89"}))
        self.assertEqual(situacao, "indicio")
        self.assertIn("AZ1117: encapsulamento: citação não menciona SOT-89; confirmado -> indicio", alertas)

    def test_encapsulamento_inventado_cai_na_regra_1(self):
        situacao, alertas = conferir_pdf("Package: SOT-223-4, DFN-8 e SOIC-8", "encapsulamento")
        self.assertEqual(situacao, "indicio")
        self.assertIn("AZ1117: encapsulamento: citação não encontrada na página", alertas)

    def test_encapsulamento_sem_campo_no_pedido_continua_fora_do_pedido(self):
        pedido = PEDIDO_SOT223.model_copy(update={"encapsulamento": None})
        situacao, alertas = conferir_pdf("Package: SOT-223-4", "encapsulamento", pedido)
        self.assertEqual(situacao, "indicio")
        self.assertIn("AZ1117: encapsulamento: requisito não existe no pedido", alertas)

    def test_evidencia_de_ciclo_de_vida_e_descartada_sem_alerta(self):
        for nome in ("ciclo de vida", "Ciclo de Vida", "ciclo_de_vida"):
            with self.subTest(nome=nome):
                evs = [evidencia(nome, "Package: SOT-223-4"), evidencia("tensão de saída", "3.3V and 5.0V. Package")]
                a = achado(mpn="AZ1117", ciclo_de_vida="ativo", evidencias=evs)
                r = conferir_achado(a, paginas(PDF), PEDIDO_SOT223)
                self.assertEqual([e.requisito for e in r.achado.evidencias], ["tensão de saída"])
                self.assertEqual(r.alertas, [])

    def test_ciclo_de_vida_descartado_tambem_com_url_nao_lida(self):
        a = achado(url="https://outro.example/x", evidencias=[evidencia("ciclo de vida", "Status: Active part")])
        self.assertEqual(conferir(a).achado.evidencias, [])


class TestRaizDoMpn(unittest.TestCase):
    """A regra da raiz, documentada por exemplos (a raiz sai normalizada: minúsculas)."""

    def test_exemplos_da_regra(self):
        casos = {
            # 1. com hífen: o que vem antes do primeiro hífen
            "AMS1117-3.3": "ams1117",
            "LT1086CM-3.3#PBF": "lt1086cm",
            # 2. sem hífen: letras + dígitos + até 2 letras de série
            "NCP1117LPST33T3G": "ncp1117lp",
            "TLV1117LV33DCYR": "tlv1117lv",
            # 3. se a regra 2 pegou o MPN inteiro: só letras + dígitos
            "LM317T": "lm317",
            # sem raiz: curta demais, igual ao próprio MPN ou começando por dígito
            "AP-12": None,  # "ap" tem menos de 4 caracteres
            "LM7": None,
            "NCP1117": None,  # já é só letras + dígitos: a "raiz" seria o próprio MPN
            "1N4007": None,
        }
        for mpn, raiz in casos.items():
            with self.subTest(mpn=mpn):
                self.assertEqual(raiz_do_mpn(mpn), raiz)

    def test_so_a_familia_na_pagina_e_valido_com_alerta(self):
        pagina = "AMS1117 series. Output voltage: 3.3 V. Output current: 1 A. SOT-223."
        evs = [evidencia("tensão de entrada máxima", citacao="Output voltage: 3.3 V", situacao="indicio")]
        r = conferir(achado(mpn="AMS1117-3.3", evidencias=evs), paginas(pagina))
        self.assertEqual(r.situacao, "valido")
        self.assertIn("AMS1117-3.3: MPN exato não encontrado na página; só a família (ams1117)", r.alertas)

    def test_familia_sem_hifen(self):
        pagina = "The NCP1117LP is a low dropout regulator."
        r = conferir(achado(mpn="NCP1117LPST33T3G", evidencias=[]), paginas(pagina))
        self.assertEqual(r.situacao, "valido")

    def test_raiz_curta_demais_recusa(self):
        r = conferir(achado(mpn="AP-12", evidencias=[]), paginas("AP regulators, AP-11 and AP-13."))
        self.assertEqual((r.situacao, r.motivo), ("recusado", "MPN não aparece na página"))

    def test_nem_mpn_nem_familia_recusa(self):
        r = conferir(achado(mpn="XY9999-3.3", evidencias=[]))
        self.assertEqual((r.situacao, r.motivo), ("recusado", "MPN não aparece na página"))

    def test_familia_nao_casa_com_outra_peca_que_so_comeca_igual(self):
        # "LM317" não pode casar com "LM3175" (outra peça) nem com "XLM317".
        for pagina in ("LM3175 buck controller", "XLM317 module"):
            with self.subTest(pagina=pagina):
                self.assertEqual(conferir(achado(mpn="LM317T", evidencias=[]), paginas(pagina)).situacao, "recusado")
        self.assertEqual(conferir(achado(mpn="LM317T", evidencias=[]), paginas("LM317 datasheet")).situacao, "valido")


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

    def test_nrnd_recusa(self):
        r = conferir(achado(ciclo_de_vida="nrnd"))
        self.assertEqual((r.situacao, r.motivo), ("recusado", "ciclo de vida: nrnd"))

    def test_desconhecido_continua_valido_com_alerta(self):
        r = conferir(achado(ciclo_de_vida="desconhecido"))
        self.assertEqual((r.situacao, r.motivo), ("valido", ""))
        self.assertEqual(r.alertas, ["AC1234: ciclo de vida desconhecido"])

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
