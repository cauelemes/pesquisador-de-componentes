"""Um CSS mínimo: as cores das situações de evidência.

Acessibilidade: a cor NUNCA vem sozinha. Cada selo tem símbolo e texto ("✔ confirmado"), para quem não distingue
cores (ou imprime em preto e branco) ler a mesma informação.

O HTML daqui é FIXO (escrito por nós): é o único lugar onde `unsafe_allow_html` aparece, e nunca recebe texto de fora.
"""

import streamlit as st

from backend.planilhas.gera_planilha import CORES_SITUACAO, ROTULOS_SITUACAO  # as mesmas cores da planilha

SIMBOLOS_SITUACAO = {"confirmado": "✔", "indicio": "?", "contradiz": "✖", "nao_encontrado": "–"}

_CSS = "<style>\n.situacao { padding: 0.1rem 0.5rem; border-radius: 0.4rem; color: #1f1f1f; white-space: nowrap; }\n" + "".join(
    f".situacao-{chave} {{ background-color: #{cor}; }}\n" for chave, cor in CORES_SITUACAO.items()
) + "</style>"


def aplicar() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def selo_situacao(situacao: str) -> None:
    """Um selo colorido com símbolo e texto. `situacao` precisa ser uma das 4 conhecidas: qualquer outra coisa vira
    texto literal (nunca entra no HTML)."""
    if situacao not in ROTULOS_SITUACAO:
        st.text(situacao)
        return
    selo = f"{SIMBOLOS_SITUACAO[situacao]} {ROTULOS_SITUACAO[situacao]}"
    st.markdown(f'<span class="situacao situacao-{situacao}">{selo}</span>', unsafe_allow_html=True)
