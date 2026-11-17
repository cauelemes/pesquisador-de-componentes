"""Ponto de entrada da interface:  streamlit run frontend/app.py

O Streamlit roda este arquivo de novo, do começo, a cada interação. Aqui só montamos o menu lateral e chamamos a seção
da etapa atual; cada seção mora num arquivo próprio em `frontend/secoes/`.
"""

import sys
from pathlib import Path

# O Streamlit põe no `sys.path` só a pasta deste arquivo (`frontend/`). Sem a RAIZ do projeto no caminho, os imports
# `backend...` e `frontend...` falham. Por isso inserimos a raiz ANTES de qualquer import do projeto.
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import streamlit as st  # noqa: E402

from backend.compartilhado.versao import VERSAO  # noqa: E402
from frontend import estado, estilo, protecao  # noqa: E402
from frontend.secoes import pedido, pesquisa, resultado, revisao  # noqa: E402

SECOES = {"pedido": pedido.render, "pesquisa": pesquisa.render, "revisao": revisao.render, "resultado": resultado.render}


def _menu() -> None:
    """Menu lateral: as etapas (a atual em destaque), o limite de pesquisas e a versão. Todos os textos são FIXOS."""
    with st.sidebar:
        st.title("Pesquisador de componentes")
        atual = estado.etapa()
        for chave, rotulo in estado.ETAPAS:
            st.markdown(f"**▶ {rotulo}**" if chave == atual else rotulo)
        st.divider()
        st.caption(f"Pesquisas restantes nesta sessão: {protecao.pesquisas_restantes()}")
        protecao.aviso_de_acesso()
        st.caption(f"Versão {VERSAO}")


st.set_page_config(page_title="Pesquisador de componentes", layout="wide")  # sempre o 1º comando do Streamlit
estilo.aplicar()
protecao.exigir_senha()  # sem a senha (quando houver uma), o script PARA aqui
pesquisa.avancar_se_concluida()
_menu()
SECOES.get(estado.etapa(), pedido.render)()
