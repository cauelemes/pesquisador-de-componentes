"""Testes dos modelos Pydantic (sem rede, sem créditos).

    python -m unittest tests.test_modelos -v

Para cada modelo conferimos três defesas: campo a mais é recusado (`extra="forbid"`), espaços das pontas são removidos
(`str_strip_whitespace`) e os limites (tamanho de texto, quantidade de itens, valores permitidos) são respeitados.
"""

import unittest

from pydantic import ValidationError

from backend.compartilhado import modelos as m


# ---------------------------------------------------------------------------------------------------------------------
# Dados válidos mínimos. Cada teste parte de um destes e muda UMA coisa, para isolar o que está sendo testado.
# ---------------------------------------------------------------------------------------------------------------------
def requisito(**mudancas) -> dict:
    return {"parametro": "tensão de entrada", "operador": ">=", "valor": 30, "unidade": "V"} | mudancas


def evidencia(**mudancas) -> dict:
    return {"requisito": "tensão de entrada", "situacao": "confirmado", "citacao": "VIN up to 36 V"} | mudancas


def achado(**mudancas) -> dict:
    return {"fabricante": "Acme Semicondutores", "mpn": "AC1234", "url": "https://acme.example/ac1234"} | mudancas


class TestResultadoBusca(unittest.TestCase):
    def test_valido_e_padroes(self):
        r = m.ResultadoBusca(url="https://a.example")
        self.assertEqual((r.titulo, r.conteudo, r.pontuacao), ("", "", None))

    def test_remove_espacos(self):
        r = m.ResultadoBusca(titulo="  Título  ", url="  https://a.example  ")
        self.assertEqual((r.titulo, r.url), ("Título", "https://a.example"))

    def test_campo_a_mais_recusado(self):
        with self.assertRaises(ValidationError):
            m.ResultadoBusca(url="https://a.example", extra=1)

    def test_url_obrigatoria_e_nao_vazia(self):
        for url in ("", "   "):  # "   " vira "" depois de tirar os espaços, e "" é curta demais
            with self.subTest(url=url), self.assertRaises(ValidationError):
                m.ResultadoBusca(url=url)
        with self.assertRaises(ValidationError):
            m.ResultadoBusca()


class TestPaginaLida(unittest.TestCase):
    def test_valido_e_padroes(self):
        p = m.PaginaLida(url="https://a.example")
        self.assertEqual((p.conteudo, p.erro, p.imagens), ("", None, []))

    def test_listas_padrao_nao_sao_compartilhadas(self):
        a, b = m.PaginaLida(url="https://a.example"), m.PaginaLida(url="https://b.example")
        a.imagens.append("x")
        self.assertEqual(b.imagens, [])

    def test_remove_espacos(self):
        self.assertEqual(m.PaginaLida(url=" https://a.example ", conteudo=" texto ").conteudo, "texto")

    def test_campo_a_mais_recusado(self):
        with self.assertRaises(ValidationError):
            m.PaginaLida(url="https://a.example", extra=1)

    def test_url_nao_vazia(self):
        with self.assertRaises(ValidationError):
            m.PaginaLida(url="  ")


class TestRequisito(unittest.TestCase):
    def test_valido(self):
        r = m.Requisito(**requisito())
        self.assertEqual((r.operador, r.valor, r.valor_max), (">=", 30.0, None))

    def test_remove_espacos(self):
        r = m.Requisito(**requisito(parametro="  tensão  ", unidade=" mV "))
        self.assertEqual((r.parametro, r.unidade), ("tensão", "mV"))

    def test_campo_a_mais_recusado(self):
        with self.assertRaises(ValidationError):
            m.Requisito(**requisito(extra=1))

    def test_operadores(self):
        for operador in (">=", "<=", "="):
            with self.subTest(operador=operador):
                m.Requisito(**requisito(operador=operador))
        for operador in (">", "<", "!=", "entre", ""):
            with self.subTest(operador=operador), self.assertRaises(ValidationError):
                m.Requisito(**requisito(operador=operador))

    def test_faixa_exige_valor_max_valido(self):
        self.assertEqual(m.Requisito(**requisito(operador="faixa", valor=1, valor_max=2)).valor_max, 2.0)
        m.Requisito(**requisito(operador="faixa", valor=1, valor_max=1))  # faixa de um ponto só: aceita
        for ruim in ({}, {"valor_max": 0.5}):  # sem valor_max / valor_max < valor
            with self.subTest(ruim=ruim), self.assertRaises(ValidationError):
                m.Requisito(**requisito(operador="faixa", valor=1, **ruim))

    def test_valor_max_so_com_faixa(self):
        with self.assertRaises(ValidationError):
            m.Requisito(**requisito(operador=">=", valor_max=40))

    def test_numero_nao_finito_recusado(self):
        for valor in (float("nan"), float("inf"), "abc"):
            with self.subTest(valor=valor), self.assertRaises(ValidationError):
                m.Requisito(**requisito(valor=valor))

    def test_limites_de_texto(self):
        m.Requisito(**requisito(parametro="x" * m.MAX_NOME, unidade="u" * m.MAX_UNIDADE))  # no limite: aceita
        for campo, tamanho in (("parametro", m.MAX_NOME + 1), ("unidade", m.MAX_UNIDADE + 1),
                               ("parametro", 0), ("unidade", 0)):
            with self.subTest(campo=campo, tamanho=tamanho), self.assertRaises(ValidationError):
                m.Requisito(**requisito(**{campo: "x" * tamanho}))


class TestPedidoComponente(unittest.TestCase):
    def test_valido(self):
        p = m.PedidoComponente(categoria=" regulador buck ", requisitos=[requisito()], encapsulamento=" SOT-23 ")
        self.assertEqual((p.categoria, p.encapsulamento), ("regulador buck", "SOT-23"))
        self.assertIsInstance(p.requisitos[0], m.Requisito)  # dicionário virou modelo

    def test_encapsulamento_opcional(self):
        self.assertIsNone(m.PedidoComponente(categoria="LDO", requisitos=[requisito()]).encapsulamento)

    def test_campo_a_mais_recusado(self):
        with self.assertRaises(ValidationError):
            m.PedidoComponente(categoria="LDO", requisitos=[requisito()], extra=1)

    def test_de_1_a_8_requisitos(self):
        m.PedidoComponente(categoria="LDO", requisitos=[requisito()] * m.MAX_REQUISITOS)
        for n in (0, m.MAX_REQUISITOS + 1):
            with self.subTest(n=n), self.assertRaises(ValidationError):
                m.PedidoComponente(categoria="LDO", requisitos=[requisito()] * n)

    def test_requisito_invalido_dentro_da_lista(self):
        with self.assertRaises(ValidationError):
            m.PedidoComponente(categoria="LDO", requisitos=[requisito(operador="?")])

    def test_limites_de_texto(self):
        for mudanca in ({"categoria": ""}, {"categoria": "x" * (m.MAX_NOME + 1)},
                        {"encapsulamento": "x" * (m.MAX_ENCAPSULAMENTO + 1)}):
            dados = {"categoria": "LDO", "requisitos": [requisito()]} | mudanca
            with self.subTest(mudanca=list(mudanca)), self.assertRaises(ValidationError):
                m.PedidoComponente(**dados)


class TestEvidenciaRequisito(unittest.TestCase):
    def test_valido_e_padroes(self):
        e = m.EvidenciaRequisito(requisito="tensão", situacao="nao_encontrado")
        self.assertEqual((e.citacao, e.valor, e.unidade, e.justificativa), ("", None, None, ""))

    def test_remove_espacos(self):
        self.assertEqual(m.EvidenciaRequisito(**evidencia(citacao="  VIN 36 V  ")).citacao, "VIN 36 V")

    def test_campo_a_mais_recusado(self):
        with self.assertRaises(ValidationError):
            m.EvidenciaRequisito(**evidencia(extra=1))

    def test_situacoes(self):
        for situacao in ("confirmado", "indicio", "nao_encontrado", "contradiz"):
            with self.subTest(situacao=situacao):
                m.EvidenciaRequisito(**evidencia(situacao=situacao))
        for situacao in ("talvez", "Confirmado", ""):
            with self.subTest(situacao=situacao), self.assertRaises(ValidationError):
                m.EvidenciaRequisito(**evidencia(situacao=situacao))

    def test_citacao_obrigatoria_exceto_em_nao_encontrado(self):
        for situacao in ("confirmado", "indicio", "contradiz"):
            for citacao in ("", "   "):  # espaços viram "" (str_strip_whitespace) e também são recusados
                with self.subTest(situacao=situacao, citacao=citacao):
                    with self.assertRaises(ValidationError) as erro:
                        m.EvidenciaRequisito(**evidencia(situacao=situacao, citacao=citacao))
                    self.assertIn("evidência 'tensão de entrada' precisa de `citacao`", str(erro.exception))
        with self.assertRaises(ValidationError):  # omitir o campo é o mesmo que deixá-lo vazio
            m.EvidenciaRequisito(requisito="tensão", situacao="confirmado", justificativa="o datasheet diz 3,3 V")
        m.EvidenciaRequisito(requisito="tensão", situacao="nao_encontrado")  # aqui pode faltar

    def test_schema_que_a_llm_ve_tambem_exige_citacao(self):
        schema = m.EvidenciaRequisito.model_json_schema()
        self.assertEqual(schema["if"], {"properties": {"situacao": {"not": {"const": "nao_encontrado"}}}})
        self.assertEqual(schema["then"]["required"], ["citacao"])
        self.assertEqual(schema["then"]["properties"]["citacao"], {"minLength": 1})
        self.assertIn("LITERALMENTE", schema["properties"]["citacao"]["description"])
        self.assertFalse(schema["additionalProperties"])  # o extra="forbid" continua lá

    def test_citacao_no_maximo_400(self):
        self.assertEqual(m.MAX_CITACAO, 400)
        m.EvidenciaRequisito(**evidencia(citacao="x" * 400))
        with self.assertRaises(ValidationError):
            m.EvidenciaRequisito(**evidencia(citacao="x" * 401))

    def test_limites_de_texto(self):
        for campo, tamanho in (("requisito", m.MAX_NOME + 1), ("requisito", 0),
                               ("unidade", m.MAX_UNIDADE + 1), ("justificativa", m.MAX_JUSTIFICATIVA + 1)):
            with self.subTest(campo=campo, tamanho=tamanho), self.assertRaises(ValidationError):
                m.EvidenciaRequisito(**evidencia(**{campo: "x" * tamanho}))

    def test_valor_nao_finito_recusado(self):
        with self.assertRaises(ValidationError):
            m.EvidenciaRequisito(**evidencia(valor=float("nan")))


class TestAchadoComponente(unittest.TestCase):
    def test_valido_e_padroes(self):
        a = m.AchadoComponente(**achado(evidencias=[evidencia()]))
        self.assertEqual(a.ciclo_de_vida, "desconhecido")
        self.assertIsInstance(a.evidencias[0], m.EvidenciaRequisito)

    def test_remove_espacos(self):
        a = m.AchadoComponente(**achado(fabricante=" Acme ", mpn=" AC1234 ", url=" https://acme.example/x "))
        self.assertEqual((a.fabricante, a.mpn, a.url), ("Acme", "AC1234", "https://acme.example/x"))

    def test_campo_a_mais_recusado(self):
        with self.assertRaises(ValidationError):
            m.AchadoComponente(**achado(extra=1))

    def test_ciclos_de_vida(self):
        for ciclo in ("ativo", "nrnd", "obsoleto", "desconhecido"):
            with self.subTest(ciclo=ciclo):
                m.AchadoComponente(**achado(ciclo_de_vida=ciclo))
        with self.assertRaises(ValidationError):
            m.AchadoComponente(**achado(ciclo_de_vida="descontinuado"))

    def test_url_so_http_ou_https(self):
        m.AchadoComponente(**achado(url="HTTP://acme.example"))  # maiúsculas no esquema: aceita
        for url in ("javascript:alert(1)", "file:///etc/passwd", "ftp://acme.example", "acme.example", "https://"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                m.AchadoComponente(**achado(url=url))

    def test_limites(self):
        casos = {
            "fabricante longo": achado(fabricante="x" * (m.MAX_FABRICANTE + 1)),
            "fabricante vazio": achado(fabricante=""),
            "mpn longo": achado(mpn="x" * (m.MAX_MPN + 1)),
            "url longa": achado(url="https://a.example/" + "x" * m.MAX_URL),
            "evidências demais": achado(evidencias=[evidencia()] * (m.MAX_EVIDENCIAS + 1)),
        }
        for nome, dados in casos.items():
            with self.subTest(nome), self.assertRaises(ValidationError):
                m.AchadoComponente(**dados)


class TestResultadoPesquisa(unittest.TestCase):
    def test_vazio_e_valido(self):
        r = m.ResultadoPesquisa()
        self.assertEqual((r.achados, r.alertas, r.resumo), ([], [], ""))

    def test_completo_e_remove_espacos(self):
        r = m.ResultadoPesquisa(achados=[achado()], alertas=["  sem datasheet  "], resumo="  ok  ")
        self.assertEqual((r.alertas, r.resumo), (["sem datasheet"], "ok"))
        self.assertIsInstance(r.achados[0], m.AchadoComponente)

    def test_campo_a_mais_recusado(self):
        with self.assertRaises(ValidationError):
            m.ResultadoPesquisa(extra=1)

    def test_limites(self):
        casos = {
            "achados demais": {"achados": [achado()] * (m.MAX_ACHADOS + 1)},
            "alertas demais": {"alertas": ["a"] * (m.MAX_ALERTAS + 1)},
            "alerta longo": {"alertas": ["x" * (m.MAX_ALERTA + 1)]},
            "alerta vazio": {"alertas": ["   "]},
            "resumo longo": {"resumo": "x" * (m.MAX_RESUMO + 1)},
        }
        for nome, dados in casos.items():
            with self.subTest(nome), self.assertRaises(ValidationError):
                m.ResultadoPesquisa(**dados)


if __name__ == "__main__":
    unittest.main()
