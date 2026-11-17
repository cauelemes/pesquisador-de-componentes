"""Etapa 1: o pedido (categoria, encapsulamento e requisitos), validado pelo modelo Pydantic `PedidoComponente`."""

import math
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from backend.compartilhado.modelos import PedidoComponente

from .. import estado, execucao, protecao

EXEMPLO = Path(__file__).resolve().parents[2] / "exemplos" / "pedido_regulador.json"
AVISO_SEM_CHAVES = "Configure TOGETHER_API_KEY e TAVILY_API_KEY no arquivo .env (veja o README)"

# Na tabela o usuário vê símbolos; o modelo usa os operadores do código.
OPERADORES = {"≥": ">=", "≤": "<=", "=": "=", "faixa": "faixa"}
SIMBOLO_DO_OPERADOR = {codigo: simbolo for simbolo, codigo in OPERADORES.items()}
COLUNAS = ["parâmetro", "operador", "valor", "valor máximo", "unidade"]
CAMPO_DA_COLUNA = {"parametro": "parâmetro", "operador": "operador", "valor": "valor", "valor_max": "valor máximo",
                   "unidade": "unidade"}


# ---------------------------------------------------------------------------------------------------------------------
# Tabela de requisitos
# ---------------------------------------------------------------------------------------------------------------------
def _tabela(linhas: list[dict]) -> pd.DataFrame:
    """As colunas numéricas viram `float64`: o vazio delas é NaN, que o editor mostra EM BRANCO. (Com `None` numa coluna
    de tipo genérico, o editor mostrava o texto "None".)"""
    return pd.DataFrame(linhas, columns=COLUNAS).astype({"valor": "float64", "valor máximo": "float64"})


def _tabela_vazia() -> pd.DataFrame:
    return _tabela([{"parâmetro": "", "operador": "≥", "valor": None, "valor máximo": None, "unidade": ""}])


def _vazio(valor) -> bool:
    """Célula vazia? O editor devolve None, "" ou NaN ("not a number", o "vazio" das colunas numéricas do pandas)."""
    return valor is None or (isinstance(valor, float) and math.isnan(valor)) or (isinstance(valor, str) and not valor.strip())


def _requisitos_da_tabela(tabela: pd.DataFrame) -> list[dict]:
    """Converte as linhas do editor em dicionários para o modelo. Linhas totalmente vazias são ignoradas."""
    requisitos = []
    for linha in tabela.to_dict("records"):
        if all(_vazio(linha.get(c)) for c in ("parâmetro", "valor", "valor máximo", "unidade")):
            continue
        valores = {campo: (None if _vazio(linha.get(coluna)) else linha.get(coluna))
                   for campo, coluna in CAMPO_DA_COLUNA.items()}
        valores["operador"] = OPERADORES.get(valores["operador"], valores["operador"])
        for campo in ("parametro", "unidade"):  # texto vazio vira "" (o modelo diz "não pode ficar vazio")
            valores[campo] = valores[campo] or ""
        requisitos.append(valores)
    return requisitos


# ---------------------------------------------------------------------------------------------------------------------
# Validação e tradução dos erros
# ---------------------------------------------------------------------------------------------------------------------
def _traduzir(erro: dict) -> str:
    """Uma mensagem do Pydantic (em inglês) em português simples."""
    tipo, ctx = erro["type"], erro.get("ctx") or {}
    if tipo == "string_too_short":
        return "não pode ficar vazio"
    if tipo == "string_too_long":
        return f"texto longo demais (máximo {ctx.get('max_length')} caracteres)"
    if tipo == "too_short":
        return "informe pelo menos 1 requisito"
    if tipo == "too_long":
        return f"no máximo {ctx.get('max_length')} requisitos"
    if tipo in ("float_parsing", "float_type", "missing"):
        return "precisa ser um número" if erro["loc"][-1] in ("valor", "valor_max") else "campo obrigatório"
    if tipo == "finite_number":
        return "precisa ser um número finito"
    if tipo == "literal_error":
        return "escolha uma das opções da lista"
    if tipo == "value_error":  # as regras do próprio modelo (já em português)
        return str(ctx.get("error", "valor inválido")).replace("valor_max", "valor máximo")
    return "valor inválido"


def _onde(loc: tuple) -> tuple[str, str]:
    """(campo da tela, descrição) de um erro. Ex.: ("requisitos", 0, "valor") -> ("requisitos", "Requisito 1, valor")."""
    if loc[0] == "requisitos" and len(loc) >= 2:
        descricao = f"Requisito {loc[1] + 1}"
        if len(loc) >= 3:
            descricao += f", {CAMPO_DA_COLUNA.get(loc[2], loc[2])}"
        return "requisitos", descricao
    return loc[0], {"categoria": "Categoria", "encapsulamento": "Encapsulamento", "requisitos": "Requisitos"}.get(
        loc[0], str(loc[0]))


def montar_pedido(categoria: str, encapsulamento: str, tabela: pd.DataFrame):
    """Devolve (pedido ou None, {campo da tela: [mensagens]})."""
    dados = {
        "categoria": categoria or "",
        "encapsulamento": (encapsulamento or "").strip() or None,  # campo vazio = sem encapsulamento
        "requisitos": _requisitos_da_tabela(tabela),
    }
    try:
        return PedidoComponente.model_validate(dados), {}
    except ValidationError as erro:
        erros: dict[str, list[str]] = {}
        for item in erro.errors():
            campo, descricao = _onde(item["loc"])
            erros.setdefault(campo, []).append(f"{descricao}: {_traduzir(item)}")
        return None, erros


# ---------------------------------------------------------------------------------------------------------------------
# Callbacks (rodam ANTES da próxima execução do script: é o jeito certo de mudar o estado a partir de um botão)
# ---------------------------------------------------------------------------------------------------------------------
def carregar_exemplo() -> None:
    pedido = PedidoComponente.model_validate_json(EXEMPLO.read_text(encoding="utf-8"))
    estado.gravar(estado.CAMPO_CATEGORIA, pedido.categoria)
    estado.gravar(estado.CAMPO_ENCAPSULAMENTO, pedido.encapsulamento or "")
    estado.gravar(estado.REQUISITOS_EDITADOS, None)
    estado.gravar(estado.REQUISITOS_BASE, _tabela([
        {"parâmetro": r.parametro, "operador": SIMBOLO_DO_OPERADOR[r.operador], "valor": r.valor,
         "valor máximo": r.valor_max, "unidade": r.unidade}
        for r in pedido.requisitos
    ]))
    # O editor guarda as EDIÇÕES pela chave; trocar a chave faz ele recomeçar a partir da tabela nova.
    estado.gravar(estado.VERSAO_DO_EDITOR, estado.ler(estado.VERSAO_DO_EDITOR, 0) + 1)


def _iniciar_pesquisa(pedido: PedidoComponente) -> None:
    # Conferimos de novo aqui: o botão desabilitado é só a interface; a regra vale no callback também.
    if execucao.chaves_faltando() or protecao.pesquisas_restantes() == 0:
        return
    estado.limpar_pesquisa()
    estado.contar_pesquisa()  # conta ao COMEÇAR: é aqui que os créditos passam a ser gastos
    estado.gravar(estado.TRABALHO, execucao.iniciar(pedido))
    estado.ir_para("pesquisa")


# ---------------------------------------------------------------------------------------------------------------------
def _mostrar_erros(lugar, mensagens: list[str]) -> None:
    if mensagens:
        lugar.error("\n\n".join(mensagens))  # mensagens montadas por nós (nomes de campos fixos), não texto de fora


def _chave_do_editor() -> str:
    return f"editor_requisitos_{estado.ler(estado.VERSAO_DO_EDITOR, 0)}"


def _restaurar_pedido() -> None:
    """Devolve aos widgets o que estava digitado, se o Streamlit apagou o estado deles (ver `estado.COPIA_*`)."""
    for campo, copia in ((estado.CAMPO_CATEGORIA, estado.COPIA_CATEGORIA),
                         (estado.CAMPO_ENCAPSULAMENTO, estado.COPIA_ENCAPSULAMENTO)):
        if campo not in st.session_state:
            estado.gravar(campo, estado.ler(copia, ""))
    # O editor guarda as edições RELATIVAS à tabela de partida. Se elas foram apagadas, a tabela editada vira a nova
    # tabela de partida (assim nada se perde e nada é aplicado duas vezes).
    if _chave_do_editor() not in st.session_state and estado.ler(estado.REQUISITOS_EDITADOS) is not None:
        estado.gravar(estado.REQUISITOS_BASE, estado.ler(estado.REQUISITOS_EDITADOS))


def _guardar_copias(tabela: pd.DataFrame) -> None:
    estado.gravar(estado.COPIA_CATEGORIA, estado.ler(estado.CAMPO_CATEGORIA, ""))
    estado.gravar(estado.COPIA_ENCAPSULAMENTO, estado.ler(estado.CAMPO_ENCAPSULAMENTO, ""))
    estado.gravar(estado.REQUISITOS_EDITADOS, tabela)


def render() -> None:
    _restaurar_pedido()
    st.header("1. Pedido")
    st.button("Carregar exemplo", key="botao_exemplo", on_click=carregar_exemplo)
    # TODO(próxima etapa): botão "Rodar demonstração (sem chaves)", que reproduz um registro salvo, sem gastar créditos.

    st.text_input("Categoria do componente", key=estado.CAMPO_CATEGORIA, placeholder="ex.: regulador linear (LDO)")
    lugar_categoria = st.empty()  # reservado: os erros só são conhecidos depois de ler todos os campos
    st.text_input("Encapsulamento (opcional)", key=estado.CAMPO_ENCAPSULAMENTO, placeholder="ex.: SOT-223")
    lugar_encapsulamento = st.empty()

    st.markdown("**Requisitos** (de 1 a 8)")
    tabela = st.data_editor(
        estado.ler(estado.REQUISITOS_BASE, _tabela_vazia()),
        key=_chave_do_editor(),
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "parâmetro": st.column_config.TextColumn("parâmetro", max_chars=80),
            "operador": st.column_config.SelectboxColumn("operador", options=list(OPERADORES), required=True),
            # "localized": o número aparece no formato do navegador (no Brasil, com vírgula decimal).
            "valor": st.column_config.NumberColumn("valor", format="localized"),
            "valor máximo": st.column_config.NumberColumn("valor máximo", format="localized",
                                                          help="só para o operador 'faixa'"),
            "unidade": st.column_config.TextColumn("unidade", max_chars=16, help="ex.: V, mA, kΩ, MHz, °C"),
        },
    )
    lugar_requisitos = st.empty()
    _guardar_copias(tabela)

    pedido, erros = montar_pedido(
        estado.ler(estado.CAMPO_CATEGORIA, ""), estado.ler(estado.CAMPO_ENCAPSULAMENTO, ""), tabela
    )
    _mostrar_erros(lugar_categoria, erros.get("categoria", []))
    _mostrar_erros(lugar_encapsulamento, erros.get("encapsulamento", []))
    _mostrar_erros(lugar_requisitos, erros.get("requisitos", []))

    sem_chaves = bool(execucao.chaves_faltando())
    if sem_chaves:
        st.warning(AVISO_SEM_CHAVES)  # avisamos ANTES: sem as chaves, a pesquisa nem começa
    sem_saldo = protecao.pesquisas_restantes() == 0
    if sem_saldo:
        st.warning("Limite de pesquisas desta sessão atingido.")
    st.button("Pesquisar", key="botao_pesquisar", type="primary", disabled=pedido is None or sem_chaves or sem_saldo,
              on_click=_iniciar_pesquisa, args=(pedido,))
