"""Leitura de quantidades com unidade ("3,3 V", "100 mA", "4,7 µF") e comparação com um `Requisito`.

Esta é a parte DETERMINÍSTICA da conferência: a LLM diz "achei 100 mA na página"; este módulo é quem decide, com
aritmética, se 100 mA atende a "corrente >= 0,05 A". Nada aqui depende de LLM.

Como funciona:
1. Separa o número e a unidade ("100 mA" -> "100" e "mA").
2. Separa o prefixo SI da unidade ("mA" -> "m" e "A").
3. Converte para a unidade-base: 100 x 10^-3 = 0,1 -> (0.1, "A").

As contas usam `Decimal` (aritmética decimal exata), e não `float`: em float, 100 * 0.001 pode dar 0.10000000000000001,
e uma comparação "= 0,1 A" falharia por um erro de arredondamento invisível.
"""

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .modelos import Requisito


class ErroDeUnidade(ValueError):
    """O texto não é uma quantidade reconhecida, ou as unidades não podem ser comparadas (ex.: V contra A)."""


# Prefixos SI aceitos, como potência de 10. "u" é o jeito comum de digitar "µ" sem teclado grego.
PREFIXOS = {"p": -12, "n": -9, "µ": -6, "u": -6, "m": -3, "k": 3, "M": 6, "G": 9}

# Unidades aceitas -> unidade-base canônica. "ohm" e "Ω" são a mesma coisa.
UNIDADES = {
    "V": "V",
    "A": "A",
    "W": "W",
    "Ω": "Ω",
    "ohm": "Ω",
    "ohms": "Ω",
    "Hz": "Hz",
    "°C": "°C",
    "F": "F",
    "H": "H",
}
SEM_PREFIXO = {"°C"}  # "m°C" ou "k°C" não fazem sentido

# Um número com no máximo UM separador decimal (ponto ou vírgula), sinal opcional, e depois a unidade.
# Não aceitamos separador de milhar: "1.000" seria ambíguo (mil ou um?). Escreva "1000" ou "1 k".
# `re.ASCII`: sem ele, `\d` também aceitaria dígitos de outros alfabetos (ex.: "٣" árabe).
_QUANTIDADE = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*(\S+)$", re.ASCII)
_TAMANHO_MAXIMO = 40  # nenhuma quantidade razoável passa disso; texto maior é lixo


def _limpar_texto(texto: str) -> str:
    """Padroniza variações de digitação de símbolos que parecem iguais, mas são caracteres diferentes."""
    # "º" (ordinal, comum no teclado brasileiro) no lugar de "°" (grau). Trocamos ANTES do NFKC, que o viraria "o".
    texto = texto.replace("º", "°")
    # NFKC junta caracteres equivalentes: "Ω" (sinal de ohm, U+2126) vira "Ω" (ômega grego), entre outros.
    texto = unicodedata.normalize("NFKC", texto)
    # O NFKC transforma "µ" (micro, U+00B5) em "μ" (mu grego, U+03BC); voltamos para o símbolo que está em PREFIXOS.
    return texto.replace("μ", "µ").strip()


def _separar_unidade(unidade: str) -> tuple[int, str]:
    """"mA" -> (-3, "A"); "V" -> (0, "V"); "kohm" -> (3, "Ω"). Levanta ErroDeUnidade se não reconhecer."""
    unidade = _limpar_texto(unidade)
    # 1º: a unidade inteira, sem prefixo ("V", "Hz", "°C"). Testar isto primeiro evita ler "H" de "Hz" como prefixo.
    if unidade in UNIDADES:
        return 0, UNIDADES[unidade]
    # 2º: primeiro caractere como prefixo e o resto como unidade ("mA", "kΩ", "MHz").
    # Não há ambiguidade: nenhum prefixo é também o início de uma unidade da tabela.
    prefixo, resto = unidade[:1], unidade[1:]
    if prefixo in PREFIXOS and resto in UNIDADES and resto not in SEM_PREFIXO:
        return PREFIXOS[prefixo], UNIDADES[resto]
    raise ErroDeUnidade(f"unidade não reconhecida: {unidade!r}")


def _para_base(numero: Decimal, unidade: str) -> tuple[Decimal, str]:
    """Converte (número, unidade com prefixo) para (número na unidade-base, unidade-base)."""
    expoente, base = _separar_unidade(unidade)
    return numero.scaleb(expoente), base  # scaleb(n) = multiplicar por 10^n, sem erro de arredondamento


def _como_decimal(valor: float) -> Decimal:
    """float -> Decimal pelo texto do número (repr), para que 0.1 vire Decimal('0.1') e não 0.1000000000000000055..."""
    if not math.isfinite(valor):
        raise ErroDeUnidade("o valor precisa ser um número finito")
    return Decimal(repr(float(valor)))


def parse_quantidade(texto: str) -> tuple[float, str]:
    """Lê "3,3 V" -> (3.3, "V"); "100 mA" -> (0.1, "A"); "2,2 kΩ" -> (2200.0, "Ω").

    Aceita vírgula ou ponto decimal, espaço opcional entre número e unidade, e os prefixos de `PREFIXOS`.
    Levanta `ErroDeUnidade` para qualquer coisa fora disso (texto lixo, unidade desconhecida, número malformado).
    """
    if not isinstance(texto, str):
        raise ErroDeUnidade("a quantidade precisa ser um texto")
    texto = _limpar_texto(texto)
    if len(texto) > _TAMANHO_MAXIMO:
        raise ErroDeUnidade("texto longo demais para ser uma quantidade")
    casamento = _QUANTIDADE.fullmatch(texto)
    if not casamento:
        raise ErroDeUnidade(f"não parece uma quantidade: {texto!r}")
    numero_txt, unidade = casamento.groups()
    try:
        numero = Decimal(numero_txt.replace(",", "."))
    except InvalidOperation:  # a regex já impede isto; fica como garantia
        raise ErroDeUnidade(f"número inválido: {numero_txt!r}") from None
    valor, base = _para_base(numero, unidade)
    return float(valor), base


def atende(requisito: Requisito, valor: float, unidade: str) -> bool:
    """O valor medido (`valor` em `unidade`) atende ao `requisito`?

    As duas pontas são convertidas para a unidade-base antes de comparar, então "100 mA" atende a ">= 0,05 A".
    Unidades de grandezas diferentes (V contra A) levantam `ErroDeUnidade`, em vez de devolver False: "não dá para
    comparar" é diferente de "não atende", e quem chama precisa saber disso.
    """
    medido, base_medida = _para_base(_como_decimal(valor), unidade)
    exigido, base_exigida = _para_base(_como_decimal(requisito.valor), requisito.unidade)
    if base_medida != base_exigida:
        raise ErroDeUnidade(f"unidades incompatíveis: {base_medida} contra {base_exigida}")

    if requisito.operador == ">=":
        return medido >= exigido
    if requisito.operador == "<=":
        return medido <= exigido
    if requisito.operador == "=":
        return medido == exigido  # exato: com Decimal, 100 mA == 0,1 A dá True
    # "faixa": os dois limites entram (intervalo fechado). O modelo `Requisito` garante que valor_max existe.
    maximo, _ = _para_base(_como_decimal(requisito.valor_max), requisito.unidade)
    return exigido <= medido <= maximo
