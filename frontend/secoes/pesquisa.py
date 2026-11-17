"""Etapa 2: a pesquisa em andamento (progresso e "Cancelar") e o desfecho (sucesso, parcial ou erro)."""

import streamlit as st

from backend.planilhas.gera_planilha import MAX_ACHADOS_NA_PLANILHA

from .. import estado, texto
from ..execucao import Trabalho


def _ir_para_revisao(trabalho: Trabalho) -> None:
    registro = trabalho.execucao.registro
    estado.gravar(estado.REGISTRO, registro)
    # Padrão: todos os válidos, até o máximo da planilha.
    estado.gravar(estado.ESCOLHIDOS, set(range(min(len(registro.resultado.achados), MAX_ACHADOS_NA_PLANILHA))))
    estado.ir_para("revisao")


def _voltar_ao_pedido() -> None:
    estado.limpar_pesquisa()
    estado.ir_para("pedido")


def avancar_se_concluida() -> None:
    """Chamada pelo `app.py` ANTES de escolher a seção: se a pesquisa acabou bem, já vai para a revisão.
    (Fica fora da seção para a troca de etapa acontecer sem um clique e sem rerun extra.)"""
    trabalho: Trabalho | None = estado.ler(estado.TRABALHO)
    if estado.etapa() != "pesquisa" or trabalho is None or trabalho.em_andamento:
        return
    if trabalho.execucao and trabalho.execucao.registro.resultado.achados and not trabalho.interrompida:
        _ir_para_revisao(trabalho)


@st.fragment(run_every=2)
def _progresso() -> None:
    """Um "fragmento": só ESTE pedaço da tela roda de novo a cada 2 s (o resto fica parado)."""
    trabalho: Trabalho | None = estado.ler(estado.TRABALHO)
    if trabalho is None:
        return
    if not trabalho.em_andamento:
        st.rerun()  # acabou: roda o app inteiro de novo, para mostrar o desfecho (ou ir para a revisão)
    orc = trabalho.orcamento  # o agente atualiza este objeto no lugar; aqui só lemos
    for nome, usado, limite in (("Passos", orc.passos_usados, orc.passos), ("Buscas", orc.buscas_usadas, orc.buscas),
                                ("Leituras", orc.leituras_usadas, orc.leituras)):
        st.progress(min(usado / limite, 1.0) if limite else 0.0, text=f"{nome}: {usado} de {limite}")
    if trabalho.cancelamento.is_set():
        st.info("Cancelando: a pesquisa para no próximo passo e devolve o que já tiver.")
    else:
        st.button("Cancelar", key="botao_cancelar", on_click=trabalho.cancelamento.set)


def _desfecho(trabalho: Trabalho) -> None:
    if trabalho.erro:
        st.error("A pesquisa não pôde ser concluída.")
        texto.mostrar(trabalho.erro)
    else:
        resultado = trabalho.execucao.registro.resultado
        if trabalho.interrompida:
            st.warning("A pesquisa foi interrompida antes do fim (cancelada, tempo esgotado ou falha da LLM). "
                       "O que já tinha sido conferido está abaixo.")
        elif not resultado.achados:
            st.warning("A pesquisa terminou sem nenhum componente válido.")
        texto.rotulo("Resumo conferido")
        texto.mostrar(resultado.resumo_conferido)
        texto.rotulo("Alertas")
        texto.mostrar_lista(resultado.alertas)
        if resultado.achados:
            st.button("Revisar o que foi encontrado", key="botao_revisar", on_click=_ir_para_revisao, args=(trabalho,))
    st.button("Voltar ao pedido", key="botao_voltar", on_click=_voltar_ao_pedido)


def render() -> None:
    st.header("2. Pesquisa")
    trabalho: Trabalho | None = estado.ler(estado.TRABALHO)
    if trabalho is None:
        st.info("Nenhuma pesquisa em andamento.")
        st.button("Voltar ao pedido", key="botao_voltar", on_click=_voltar_ao_pedido)
        return
    if trabalho.em_andamento:
        st.caption("Pesquisando na web e conferindo as provas. Isto pode levar alguns minutos.")
        _progresso()
    else:
        _desfecho(trabalho)
