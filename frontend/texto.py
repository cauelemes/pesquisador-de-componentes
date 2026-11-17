"""Como mostrar texto na tela com segurança.

Texto vindo da web ou da LLM é DADO. Se ele fosse para `st.markdown` ou `st.write`, um "**x**" viraria negrito, um
link falso viraria clicável e, com `unsafe_allow_html`, um "<script>" poderia rodar. Por isso:
- texto de fora: SEMPRE `mostrar` (st.text) ou `mostrar_copiavel` (st.code), que exibem os caracteres como estão;
- `rotulo` (markdown) só para textos FIXOS escritos por nós.
"""

import streamlit as st


def mostrar(texto: str | None, vazio: str = "—") -> None:
    """Texto de fora, literal."""
    st.text(texto if texto else vazio)


def mostrar_copiavel(texto: str) -> None:
    """Texto de fora, literal, numa caixa com botão de copiar (ex.: uma URL, que NÃO vira link clicável)."""
    st.code(texto, language=None)


def mostrar_lista(textos: list[str], vazio: str = "(nenhum)") -> None:
    """Uma lista de textos de fora, um por linha."""
    if not textos:
        st.text(vazio)
    for texto in textos:
        st.text(f"• {texto}")


def rotulo(fixo: str) -> None:
    """Um rótulo em negrito. SÓ para texto FIXO nosso (nunca passe aqui algo vindo da web ou da LLM)."""
    st.markdown(f"**{fixo}**")
