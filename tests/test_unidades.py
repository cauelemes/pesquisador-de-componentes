"""Testes da leitura de quantidades e da comparação com requisitos (sem rede, sem créditos).

    python -m unittest tests.test_unidades -v
"""

import unittest

from backend.compartilhado.modelos import Requisito
from backend.compartilhado.unidades import ErroDeUnidade, atende, parse_quantidade


def req(operador: str, valor: float, unidade: str, valor_max: float | None = None) -> Requisito:
    """Atalho para montar um Requisito nos testes."""
    return Requisito(parametro="p", operador=operador, valor=valor, valor_max=valor_max, unidade=unidade)


class TestParseQuantidade(unittest.TestCase):
    def test_exemplos_basicos(self):
        self.assertEqual(parse_quantidade("3,3 V"), (3.3, "V"))
        self.assertEqual(parse_quantidade("100 mA"), (0.1, "A"))  # exato, graças ao Decimal

    def test_virgula_ponto_e_espaco(self):
        for texto in ("3,3 V", "3.3 V", "3,3V", "  3,3   V  "):
            with self.subTest(texto=texto):
                self.assertEqual(parse_quantidade(texto), (3.3, "V"))

    def test_sinal(self):
        self.assertEqual(parse_quantidade("-40 °C"), (-40.0, "°C"))
        self.assertEqual(parse_quantidade("+5 V"), (5.0, "V"))

    def test_todos_os_prefixos(self):
        casos = {
            "1 pF": 1e-12, "1 nF": 1e-9, "1 µF": 1e-6, "1 uF": 1e-6, "1 mF": 1e-3,
            "1 kF": 1e3, "1 MF": 1e6, "1 GF": 1e9,
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(parse_quantidade(texto), (esperado, "F"))

    def test_micro_em_suas_duas_grafias(self):
        # "µ" (sinal de micro, U+00B5) e "μ" (letra grega mu, U+03BC) parecem iguais; os dois são aceitos.
        self.assertEqual(parse_quantidade("4,7 µF"), (4.7e-6, "F"))
        self.assertEqual(parse_quantidade("4,7 μF"), (4.7e-6, "F"))

    def test_todas_as_unidades(self):
        casos = {
            "5 V": (5.0, "V"), "2 A": (2.0, "A"), "1 W": (1.0, "W"), "10 Hz": (10.0, "Hz"),
            "85 °C": (85.0, "°C"), "85 ºC": (85.0, "°C"),  # "º" ordinal, comum no teclado brasileiro
            "10 F": (10.0, "F"), "10 H": (10.0, "H"),
            "10 Ω": (10.0, "Ω"), "10 Ω": (10.0, "Ω"),  # U+2126 é o "sinal de ohm"
            "10 ohm": (10.0, "Ω"), "10 ohms": (10.0, "Ω"),
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(parse_quantidade(texto), esperado)

    def test_prefixo_com_unidades_de_mais_de_uma_letra(self):
        self.assertEqual(parse_quantidade("2,2 kΩ"), (2200.0, "Ω"))
        self.assertEqual(parse_quantidade("2,2 kohm"), (2200.0, "Ω"))
        self.assertEqual(parse_quantidade("16 MHz"), (16e6, "Hz"))
        self.assertEqual(parse_quantidade("10 mH"), (0.01, "H"))  # "mH" é mili-henry, não confunde com Hz

    def test_texto_lixo(self):
        lixo = [
            "", "   ", "abc", "V", "3,3", "33", "3,3 X", "3,3 v", "3.3.3 V", "1.000,5 V", "1e5 V",
            "10 kV extra", "m°C", "5 m°C", "5 KV", "5 mm", "٣ V", "3,3 V" + " " * 50 + "x", "1" * 50 + " V",
        ]
        for texto in lixo:
            with self.subTest(texto=texto), self.assertRaises(ErroDeUnidade):
                parse_quantidade(texto)

    def test_nao_texto(self):
        for valor in (None, 3.3, b"3 V"):
            with self.subTest(valor=valor), self.assertRaises(ErroDeUnidade):
                parse_quantidade(valor)

    def test_erro_e_value_error(self):
        # Quem quiser tratar de forma genérica pode capturar ValueError.
        self.assertTrue(issubclass(ErroDeUnidade, ValueError))


class TestAtende(unittest.TestCase):
    def test_maior_ou_igual(self):
        r = req(">=", 30, "V")
        self.assertTrue(atende(r, 36, "V"))
        self.assertTrue(atende(r, 30, "V"))  # o limite entra
        self.assertFalse(atende(r, 29.9, "V"))

    def test_menor_ou_igual(self):
        r = req("<=", 1, "mA")
        self.assertTrue(atende(r, 500, "µA"))
        self.assertTrue(atende(r, 0.001, "A"))
        self.assertFalse(atende(r, 2, "mA"))

    def test_igual_entre_prefixos_diferentes(self):
        # Em float, 100 * 0.001 != 0.1; com Decimal, a igualdade é exata.
        self.assertTrue(atende(req("=", 0.1, "A"), 100, "mA"))
        self.assertTrue(atende(req("=", 2.2, "kΩ"), 2200, "ohm"))
        self.assertFalse(atende(req("=", 3.3, "V"), 3.31, "V"))

    def test_faixa(self):
        r = req("faixa", 4.5, "V", valor_max=5.5)
        for valor, esperado in ((4.5, True), (5, True), (5.5, True), (4.49, False), (5.51, False)):
            with self.subTest(valor=valor):
                self.assertEqual(atende(r, valor, "V"), esperado)

    def test_faixa_com_prefixo(self):
        r = req("faixa", 100, "kHz", valor_max=2000)  # de 100 kHz a 2 MHz
        self.assertTrue(atende(r, 1, "MHz"))
        self.assertFalse(atende(r, 50, "kHz"))
        self.assertFalse(atende(r, 3, "MHz"))

    def test_temperatura_negativa(self):
        r = req("faixa", -40, "°C", valor_max=85)
        self.assertTrue(atende(r, -40, "°C"))
        self.assertFalse(atende(r, -55, "°C"))

    def test_unidade_incompativel(self):
        for unidade in ("A", "mA", "W", "Hz"):
            with self.subTest(unidade=unidade), self.assertRaises(ErroDeUnidade):
                atende(req(">=", 30, "V"), 36, unidade)

    def test_unidade_desconhecida(self):
        with self.assertRaises(ErroDeUnidade):
            atende(req(">=", 30, "V"), 36, "volts")
        with self.assertRaises(ErroDeUnidade):
            atende(req(">=", 8, "pinos"), 8, "pinos")

    def test_valor_nao_finito(self):
        for valor in (float("nan"), float("inf")):
            with self.subTest(valor=valor), self.assertRaises(ErroDeUnidade):
                atende(req(">=", 30, "V"), valor, "V")


if __name__ == "__main__":
    unittest.main()
