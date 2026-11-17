"""Etapa 4: os arquivos para baixar (planilha e pacote .zip), o resumo de uso e "Nova pesquisa"."""

import zipfile
from io import BytesIO

import streamlit as st

from backend.servico import pesquisa as servico

from .. import estado

TIPO_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _nova_pesquisa() -> None:
    estado.limpar_pesquisa()  # o pedido digitado fica, para ajustar e pesquisar de novo
    estado.ir_para("pedido")


def _inteiro(n: int) -> str:
    return f"{n:,}".replace(",", ".")  # 12345 -> "12.345"


def render() -> None:
    st.header("4. Resultado")
    pacote, registro = estado.ler(estado.PACOTE), estado.ler(estado.REGISTRO)
    if pacote is None or registro is None:
        st.info("Nenhum resultado gerado ainda.")
        st.button("Nova pesquisa", key="botao_nova", on_click=_nova_pesquisa)
        return

    planilha = zipfile.ZipFile(BytesIO(pacote)).read(servico.ARQUIVO_PLANILHA)  # a mesma planilha que está no pacote
    # Nomes de arquivo FIXOS (nunca derivados de texto da web ou do usuário).
    st.download_button("Baixar planilha (.xlsx)", data=planilha, file_name="ficha_de_componentes.xlsx",
                       mime=TIPO_XLSX, key="baixar_planilha")
    st.download_button("Baixar pacote completo (.zip)", data=pacote, file_name="pesquisa_de_componentes.zip",
                       mime="application/zip", key="baixar_pacote")

    st.subheader("Uso desta pesquisa")
    uso = registro.uso
    entrada, saida, custo, duracao = st.columns(4)
    entrada.metric("Tokens de entrada", _inteiro(uso.tokens_entrada), help=f"{_inteiro(uso.tokens_em_cache)} em cache")
    saida.metric("Tokens de saída", _inteiro(uso.tokens_saida), help=f"{_inteiro(uso.tokens_raciocinio)} de raciocínio")
    custo.metric("Custo estimado", f"US$ {uso.custo_estimado_usd:.4f}".replace(".", ","))
    duracao.metric("Duração", f"{registro.duracao_segundos:.0f} s")
    st.caption("Custo estimado com os preços de referência do código; confira a tabela de preços da Together.")

    st.button("Nova pesquisa", key="botao_nova", type="primary", on_click=_nova_pesquisa)
