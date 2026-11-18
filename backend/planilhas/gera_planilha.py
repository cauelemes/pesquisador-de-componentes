"""Gera a planilha .xlsx com os componentes escolhidos pelo usuário (em memória: nada de arquivo temporário).

Três abas:
- "Alternativas": um componente por linha, com uma coluna por requisito do pedido ("3,3 V (confirmado)").
- "Evidências": uma evidência por linha, com a citação literal (a prova) e a URL.
- "Pedido e alertas": o pedido, o resumo CONFERIDO, os recusados com o motivo, o comentário da LLM (rotulado como
  não conferido) e os alertas.

SEGURANÇA: quase tudo aqui veio da web ou da LLM. Uma planilha é um alvo clássico de "injeção de fórmula": um texto como
`=HYPERLINK("http://site-malicioso", "clique")` vira FÓRMULA ao ser gravado, e o Excel a executa ao abrir. Por isso todo
texto passa por `_texto_seguro` antes de entrar numa célula. URLs ficam como texto puro (sem hiperlink).
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

from ..compartilhado.modelos import (
    ROTULO_RESUMO_DA_LLM,
    AchadoComponente,
    AchadoRecusado,
    EvidenciaRequisito,
    PedidoComponente,
)
from ..pesquisa.conferencia import NOMES_ENCAPSULAMENTO, normalizar_texto

MAX_ACHADOS_NA_PLANILHA = 5
AVISO_DEMONSTRACAO = "Gerado a partir de uma execução gravada (modo demonstração): nenhuma API foi chamada."
MAX_CARACTERES_CELULA = 32_000  # o Excel aceita até 32.767; ficamos um pouco abaixo
# Começos que fazem o Excel (ou o LibreOffice) tratar o texto como fórmula. Tab e retorno de carro também entram na
# lista da OWASP ("CSV injection"), porque alguns programas os ignoram e leem o que vem depois.
_INICIO_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")

ROTULOS_SITUACAO = {
    "confirmado": "confirmado",
    "indicio": "indício",
    "contradiz": "contradiz",
    "nao_encontrado": "não encontrado",
}
# Cores suaves (fundo da célula) por situação.
CORES_SITUACAO = {
    "confirmado": "C6EFCE",  # verde claro
    "indicio": "FFEB9C",  # amarelo claro
    "contradiz": "FFC7CE",  # vermelho claro
    "nao_encontrado": "E7E6E6",  # cinza claro
}
# Como mostrar cada operador ao leitor ("=" sozinho numa célula pareceria o início de uma fórmula).
ROTULOS_OPERADOR = {">=": "≥", "<=": "≤", "=": "igual a", "faixa": "faixa"}
_CABECALHO_FONTE = Font(bold=True)
_CABECALHO_FUNDO = PatternFill("solid", fgColor="D9E1F2")  # azul acinzentado
_QUEBRA = Alignment(wrap_text=True, vertical="top")
_TOPO = Alignment(vertical="top")


class ErroDePlanilha(ValueError):
    """Dados que não dá para transformar em planilha (ex.: nenhum achado ou achados demais)."""


# =====================================================================================================================
# Utilidades
# =====================================================================================================================
def _texto_seguro(valor) -> str:
    """Converte qualquer coisa em texto SEGURO para uma célula.

    1. Remove caracteres de controle que o formato .xlsx não aceita (o openpyxl levantaria erro).
    2. Corta no limite de tamanho de uma célula.
    3. Se começar como fórmula (=, +, -, @...), põe um apóstrofo na frente: vira texto, nunca fórmula.
    """
    texto = ILLEGAL_CHARACTERS_RE.sub("", "" if valor is None else str(valor))
    texto = texto[:MAX_CARACTERES_CELULA]
    if texto.startswith(_INICIO_DE_FORMULA):
        texto = "'" + texto[: MAX_CARACTERES_CELULA - 1]
    return texto


def _numero_br(valor: float) -> str:
    """3.3 -> "3,3"; 1.0 -> "1"; 0.0001 -> "0,0001". Só para TEXTO visível; os números das células continuam números."""
    texto = f"{valor:.6f}".rstrip("0").rstrip(".") if abs(valor) >= 1e-6 or valor == 0 else f"{valor:g}"
    return texto.replace(".", ",")


def _escrever_linha(aba: Worksheet, valores: list) -> None:
    """Acrescenta uma linha. Números (int/float) ficam números; TODO o resto passa por `_texto_seguro`, inclusive os
    textos fixos nossos (não custa nada e evita esquecer um caso)."""
    aba.append([v if isinstance(v, (int, float)) and not isinstance(v, bool) else _texto_seguro(v) for v in valores])


def _formatar_cabecalho(aba: Worksheet, larguras: list[int], colunas_longas: set[int]) -> None:
    """Cabeçalho em negrito com fundo, primeira linha congelada, larguras e quebra de texto nas colunas longas."""
    for celula in aba[1]:
        celula.font = _CABECALHO_FONTE
        celula.fill = _CABECALHO_FUNDO
        celula.alignment = _QUEBRA
    aba.freeze_panes = "A2"  # congela a linha 1: o cabeçalho fica visível ao rolar
    for indice, largura in enumerate(larguras, start=1):
        aba.column_dimensions[aba.cell(row=1, column=indice).column_letter].width = largura
    for linha in aba.iter_rows(min_row=2):
        for celula in linha:
            celula.alignment = _QUEBRA if celula.column - 1 in colunas_longas else _TOPO


def _pintar(celula, situacao: str | None) -> None:
    if situacao in CORES_SITUACAO:
        celula.fill = PatternFill("solid", fgColor=CORES_SITUACAO[situacao])


def _evidencia_de(achado: AchadoComponente, nomes: set[str]) -> EvidenciaRequisito | None:
    """A primeira evidência do achado cujo nome (normalizado) está em `nomes`."""
    return next((e for e in achado.evidencias if normalizar_texto(e.requisito) in nomes), None)


def _celula_de_requisito(evidencia: EvidenciaRequisito | None) -> str:
    """O texto da célula de um requisito: "3,3 V (confirmado)", "(indício)" ou "sem evidência"."""
    if evidencia is None:
        return "sem evidência"
    rotulo = f"({ROTULOS_SITUACAO[evidencia.situacao]})"
    if evidencia.valor is None:
        return rotulo
    unidade = f" {evidencia.unidade}" if evidencia.unidade else ""
    return f"{_numero_br(evidencia.valor)}{unidade} {rotulo}"


# =====================================================================================================================
# Abas
# =====================================================================================================================
def _aba_alternativas(aba: Worksheet, pedido: PedidoComponente, achados: list[AchadoComponente]) -> None:
    fixas = ["Nº", "Fabricante", "MPN", "Ciclo de vida", "URL da fonte"]
    requisitos = [
        f"{r.parametro} ({r.operador} {_numero_br(r.valor)}"
        + (f" a {_numero_br(r.valor_max)}" if r.valor_max is not None else "")
        + f" {r.unidade})"
        for r in pedido.requisitos
    ]
    encapsulamento = [f"Encapsulamento ({pedido.encapsulamento})"] if pedido.encapsulamento else []
    _escrever_linha(aba, fixas + requisitos + encapsulamento)  # cabeçalho: o parâmetro vem do pedido -> texto seguro

    for numero, achado in enumerate(achados, start=1):
        evidencias = [_evidencia_de(achado, {normalizar_texto(r.parametro)}) for r in pedido.requisitos]
        if pedido.encapsulamento:
            evidencias.append(_evidencia_de(achado, NOMES_ENCAPSULAMENTO))
        celulas = [_celula_de_requisito(e) for e in evidencias]
        _escrever_linha(aba, [numero, achado.fabricante, achado.mpn, achado.ciclo_de_vida, achado.url, *celulas])
        for deslocamento, evidencia in enumerate(evidencias):
            _pintar(aba.cell(row=aba.max_row, column=len(fixas) + 1 + deslocamento), evidencia and evidencia.situacao)

    colunas = len(fixas) + len(requisitos) + len(encapsulamento)
    _formatar_cabecalho(aba, [5, 22, 22, 14, 45] + [24] * (colunas - len(fixas)), colunas_longas={4})


def _aba_evidencias(aba: Worksheet, achados: list[AchadoComponente]) -> None:
    _escrever_linha(aba, ["MPN", "Requisito", "Situação", "Valor", "Unidade", "Citação literal", "Justificativa", "URL"])
    for achado in achados:
        for e in achado.evidencias:
            valor = e.valor if e.valor is not None else ""  # número de verdade na célula (sem fórmula)
            _escrever_linha(
                aba,
                [achado.mpn, e.requisito, ROTULOS_SITUACAO[e.situacao], valor, e.unidade, e.citacao, e.justificativa,
                 achado.url],
            )
            _pintar(aba.cell(row=aba.max_row, column=3), e.situacao)
    _formatar_cabecalho(aba, [22, 24, 16, 10, 10, 60, 50, 45], colunas_longas={5, 6, 7})


def _aba_pedido(
    aba: Worksheet,
    pedido: PedidoComponente,
    resumo_conferido: str,
    recusados: list[AchadoRecusado],
    resumo_da_llm: str,
    alertas: list[str],
    demonstracao: bool,
) -> None:
    _escrever_linha(aba, ["Campo", "Valor"])
    if demonstracao:  # antes de tudo: quem abrir a planilha precisa saber que ela não veio de uma pesquisa nova
        _escrever_linha(aba, ["Atenção", AVISO_DEMONSTRACAO])
        aba.cell(row=aba.max_row, column=1).font = _CABECALHO_FONTE
    _escrever_linha(aba, ["Categoria", pedido.categoria])
    _escrever_linha(aba, ["Encapsulamento", pedido.encapsulamento or "(não especificado)"])

    aba.append([])
    _escrever_linha(aba, ["Requisitos", "Operador", "Valor", "Valor máximo", "Unidade"])
    for celula in aba[aba.max_row]:
        celula.font = _CABECALHO_FONTE
    for r in pedido.requisitos:
        _escrever_linha(aba, [r.parametro, ROTULOS_OPERADOR[r.operador], r.valor, r.valor_max if r.valor_max is not None else "", r.unidade])

    aba.append([])
    # Ordem de propósito: primeiro o que o CÓDIGO conferiu; o texto livre da LLM vem depois, com o rótulo.
    _escrever_linha(aba, ["Resumo conferido", resumo_conferido or "(sem resumo)"])
    aba.cell(row=aba.max_row, column=1).font = _CABECALHO_FONTE

    aba.append([])
    _escrever_linha(aba, ["Recusados na conferência", "" if recusados else "(nenhum)"])
    aba.cell(row=aba.max_row, column=1).font = _CABECALHO_FONTE
    for r in recusados:
        _escrever_linha(aba, [f"{r.mpn} ({r.fabricante})", r.motivo])

    aba.append([])
    _escrever_linha(aba, [ROTULO_RESUMO_DA_LLM, resumo_da_llm or "(sem comentário)"])
    aba.cell(row=aba.max_row, column=1).font = _CABECALHO_FONTE

    aba.append([])
    _escrever_linha(aba, ["Alertas", "" if alertas else "(nenhum)"])
    aba.cell(row=aba.max_row, column=1).font = _CABECALHO_FONTE
    for alerta in alertas:
        _escrever_linha(aba, ["", alerta])
    _formatar_cabecalho(aba, [28, 80, 14, 14, 10], colunas_longas={1})


# =====================================================================================================================
def gerar_planilha(
    pedido: PedidoComponente,
    achados: list[AchadoComponente],
    resumo_conferido: str,
    alertas: list[str],
    *,
    recusados: list[AchadoRecusado] = (),
    resumo_da_llm: str = "",
    demonstracao: bool = False,
) -> bytes:
    """Monta a planilha e devolve os BYTES do .xlsx (para baixar ou pôr num .zip).

    `resumo_conferido` é o texto do CÓDIGO; `resumo_da_llm` vai em outra linha, rotulado como não conferido.

    Raises:
        ErroDePlanilha: nenhum achado, ou mais de 5.
    """
    if not 1 <= len(achados) <= MAX_ACHADOS_NA_PLANILHA:
        raise ErroDePlanilha(f"escolha de 1 a {MAX_ACHADOS_NA_PLANILHA} componentes (veio {len(achados)})")

    livro = Workbook()
    alternativas = livro.active  # um Workbook novo já vem com uma aba; renomeamos essa
    alternativas.title = "Alternativas"
    _aba_alternativas(alternativas, pedido, achados)
    _aba_evidencias(livro.create_sheet("Evidências"), achados)
    _aba_pedido(livro.create_sheet("Pedido e alertas"), pedido, resumo_conferido, list(recusados), resumo_da_llm, alertas,
                demonstracao)

    memoria = BytesIO()  # um "arquivo" na memória: nada é gravado em disco
    livro.save(memoria)
    return memoria.getvalue()
