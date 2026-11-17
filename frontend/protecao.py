"""Proteção do acesso: senha OPCIONAL e limite de pesquisas por sessão.

- Senha: se `senha_de_acesso` existir em `st.secrets` (arquivo `.streamlit/secrets.toml`, fora do git), a tela pede a
  senha antes de tudo. Sem ela, o acesso fica aberto (com um aviso discreto no menu lateral).
- Limite: cada pesquisa gasta créditos; uma sessão (aba do navegador) pode fazer no máximo MAX_PESQUISAS_POR_SESSAO.
  É um freio contra uso acidental, não uma cota segura: recarregar a página começa uma sessão nova.
"""

import hmac

import streamlit as st

from . import estado

MAX_PESQUISAS_POR_SESSAO = 10


def _senha_dos_segredos() -> str | None:
    """A senha configurada, ou None. Isolada numa função para os testes poderem trocá-la (sem ler o arquivo real)."""
    try:
        senha = st.secrets.get("senha_de_acesso")
    except Exception:  # sem `secrets.toml`, o Streamlit levanta erro já na leitura
        return None
    return senha if isinstance(senha, str) and senha else None


def _conferir_senha() -> None:
    """Callback do botão "Entrar". `hmac.compare_digest` compara em tempo constante: o tempo da comparação não revela
    quantos caracteres do começo estavam certos (o que um `==` comum poderia revelar)."""
    senha = _senha_dos_segredos() or ""
    digitada = estado.ler(estado.CAMPO_SENHA, "") or ""
    certa = bool(senha) and hmac.compare_digest(digitada.encode("utf-8"), senha.encode("utf-8"))
    estado.gravar(estado.AUTENTICADO, certa)
    estado.gravar(estado.SENHA_ERRADA, not certa)
    estado.gravar(estado.CAMPO_SENHA, "")  # não deixa a senha digitada guardada no estado


def exigir_senha() -> None:
    """Se houver senha configurada e a sessão ainda não entrou, mostra a tela de senha e PARA o script aqui."""
    if _senha_dos_segredos() is None or estado.ler(estado.AUTENTICADO):
        return
    st.title("Pesquisador de componentes eletrônicos")
    st.text_input("Senha de acesso", type="password", key=estado.CAMPO_SENHA)
    st.button("Entrar", key="botao_entrar", on_click=_conferir_senha)
    if estado.ler(estado.SENHA_ERRADA):
        st.error("Senha incorreta.")
    st.stop()  # nada abaixo roda: o resto do app fica invisível sem a senha


def aviso_de_acesso() -> None:
    """No menu lateral: avisa (discretamente) quando não há senha."""
    if _senha_dos_segredos() is None:
        st.sidebar.caption("Acesso aberto: nenhuma senha configurada.")


def pesquisas_restantes() -> int:
    return max(0, MAX_PESQUISAS_POR_SESSAO - estado.pesquisas_feitas())
