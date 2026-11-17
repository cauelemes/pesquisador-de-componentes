"""Etapa 3: revisão humana. O usuário vê os achados válidos com as provas e escolhe de 1 a 5 para a planilha.

Tudo o que veio da web ou da LLM aparece com `texto.mostrar` (literal). Rótulos de widgets e de expanders são FIXOS:
o rótulo de um widget é interpretado como markdown, então nunca leva texto de fora.
"""

import streamlit as st

from backend.compartilhado.modelos import ROTULO_RESUMO_DA_LLM, AchadoComponente
from backend.planilhas.gera_planilha import MAX_ACHADOS_NA_PLANILHA
from backend.servico import pesquisa as servico

from .. import estado, estilo, texto

_LARGURAS = [2, 1.6, 1.2, 4, 3]  # requisito, situação, valor, citação, justificativa


def _chave_da_caixa(indice: int) -> str:
    # O número da pesquisa entra na chave: numa pesquisa nova, as caixas recomeçam (não herdam marcações antigas).
    return f"escolha_{estado.pesquisas_feitas()}_{indice}"


def _alternar(indice: int) -> None:
    """Callback da caixa: a ESCOLHA mora em `estado.ESCOLHIDOS`; a caixa só a reflete."""
    escolhidos = set(estado.escolhidos())
    if estado.ler(_chave_da_caixa(indice)):
        escolhidos.add(indice)
    else:
        escolhidos.discard(indice)
    estado.gravar(estado.ESCOLHIDOS, escolhidos)


def _gerar() -> None:
    registro = estado.ler(estado.REGISTRO)
    achados = [registro.resultado.achados[i] for i in sorted(estado.escolhidos())]
    try:
        estado.gravar(estado.PACOTE, servico.montar_pacote(registro, achados))
    except servico.ErroDeEscolha as erro:  # mensagem nossa, para o usuário
        estado.gravar(estado.ERRO_AO_GERAR, str(erro))
        return
    estado.gravar(estado.ERRO_AO_GERAR, None)
    estado.ir_para("resultado")


def _voltar_ao_pedido() -> None:
    estado.limpar_pesquisa()
    estado.ir_para("pedido")


def _numero(valor: float) -> str:
    return f"{valor:g}".replace(".", ",")


def _achado(indice: int, achado: AchadoComponente) -> None:
    with st.container(border=True):
        texto.rotulo(f"Componente {indice + 1}")
        colunas = st.columns(3)
        for coluna, rotulo, valor in zip(colunas, ("Fabricante", "MPN", "Ciclo de vida"),
                                         (achado.fabricante, achado.mpn, achado.ciclo_de_vida)):
            with coluna:
                st.caption(rotulo)
                texto.mostrar(valor)
        st.caption("Fonte (copie o endereço; ele não vira link de propósito)")
        texto.mostrar_copiavel(achado.url)

        cabecalho = st.columns(_LARGURAS)
        for coluna, rotulo in zip(cabecalho, ("Requisito", "Situação", "Valor", "Citação literal", "Justificativa")):
            coluna.caption(rotulo)
        for evidencia in achado.evidencias:
            requisito, situacao, valor, citacao, justificativa = st.columns(_LARGURAS)
            with requisito:
                texto.mostrar(evidencia.requisito)
            with situacao:
                estilo.selo_situacao(evidencia.situacao)
            with valor:
                unidade = f" {evidencia.unidade}" if evidencia.unidade else ""
                texto.mostrar(f"{_numero(evidencia.valor)}{unidade}" if evidencia.valor is not None else "")
            with citacao:
                texto.mostrar(evidencia.citacao)
            with justificativa:
                texto.mostrar(evidencia.justificativa)

        chave = _chave_da_caixa(indice)
        st.checkbox("Incluir na planilha", key=chave, value=indice in estado.escolhidos(),
                    on_change=_alternar, args=(indice,))


def render() -> None:
    st.header("3. Revisão")
    registro = estado.ler(estado.REGISTRO)
    if registro is None:
        st.info("Nenhuma pesquisa para revisar.")
        st.button("Voltar ao pedido", key="botao_voltar", on_click=_voltar_ao_pedido)
        return
    resultado = registro.resultado

    texto.rotulo("Resumo conferido")
    texto.mostrar(resultado.resumo_conferido)

    for indice, achado in enumerate(resultado.achados):
        _achado(indice, achado)

    with st.expander("Recusados na conferência"):  # rótulo FIXO
        if not resultado.recusados:
            texto.mostrar("(nenhum)")
        for recusado in resultado.recusados:
            texto.mostrar(f"{recusado.mpn} ({recusado.fabricante}): {recusado.motivo}")

    texto.rotulo("Alertas")
    texto.mostrar_lista(resultado.alertas)

    with st.expander(ROTULO_RESUMO_DA_LLM):
        texto.mostrar(resultado.resumo_da_llm, vazio="(sem comentário)")

    quantos = len(estado.escolhidos())
    pode_gerar = 1 <= quantos <= MAX_ACHADOS_NA_PLANILHA
    if not pode_gerar:
        st.caption(f"Escolha de 1 a {MAX_ACHADOS_NA_PLANILHA} componentes para gerar o resultado "
                   f"(escolhidos: {quantos}).")
    if estado.ler(estado.ERRO_AO_GERAR):
        st.error("Não foi possível gerar o resultado.")
        texto.mostrar(estado.ler(estado.ERRO_AO_GERAR))
    st.button("Gerar resultado", key="botao_gerar", type="primary", disabled=not pode_gerar, on_click=_gerar)
    st.button("Voltar ao pedido", key="botao_voltar", on_click=_voltar_ao_pedido)
