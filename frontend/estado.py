"""O estado da interface: as chaves de `st.session_state` e funções para ler e gravar.

Como o Streamlit funciona: a CADA clique, o script `app.py` roda de novo do começo. O que precisa sobreviver entre
uma execução e outra fica em `st.session_state` (um dicionário por aba do navegador). Para não espalhar nomes de
chaves pelo código, todas ficam aqui.

ATENÇÃO: `st.session_state` NÃO tem `setdefault`; usamos `.get` para ler e atribuição para gravar.
"""

import streamlit as st

# As quatro etapas, em ordem: (chave interna, rótulo FIXO mostrado no menu lateral).
ETAPAS = (("pedido", "1. Pedido"), ("pesquisa", "2. Pesquisa"), ("revisao", "3. Revisão"), ("resultado", "4. Resultado"))

# Chaves do estado.
ETAPA = "etapa"
TRABALHO = "trabalho"  # o `execucao.Trabalho` da pesquisa em andamento (ou terminada)
REGISTRO = "registro"  # o `RegistroPesquisa` da pesquisa concluída
ESCOLHIDOS = "escolhidos"  # set com os ÍNDICES dos achados escolhidos para a planilha
PACOTE = "pacote"  # os bytes do .zip gerado
ERRO_AO_GERAR = "erro_ao_gerar"
PESQUISAS_FEITAS = "pesquisas_feitas"
AUTENTICADO = "autenticado"
SENHA_ERRADA = "senha_errada"
REQUISITOS_BASE = "requisitos_base"  # a tabela de partida do editor de requisitos
# Cópias do pedido em chaves COMUNS. O Streamlit apaga o estado de um widget quando ele não é desenhado numa execução
# (ex.: enquanto o usuário está na revisão); estas cópias sobrevivem e devolvem os valores ao voltar ao pedido.
COPIA_CATEGORIA = "copia_categoria"
COPIA_ENCAPSULAMENTO = "copia_encapsulamento"
REQUISITOS_EDITADOS = "requisitos_editados"
VERSAO_DO_EDITOR = "versao_do_editor"  # muda para o editor recomeçar do zero (ver `secoes.pedido`)
PEDIDO_TOCADO = "pedido_tocado"  # o usuário já editou algo? (só então os erros do pedido aparecem)
DEMONSTRACAO = "demonstracao"  # a pesquisa atual é a REPETIÇÃO de uma gravação (aviso no topo de todas as etapas)
ERRO_DEMONSTRACAO = "erro_demonstracao"

# Chaves de widgets (o Streamlit guarda o valor do widget em session_state[chave]).
CAMPO_CATEGORIA = "campo_categoria"
CAMPO_ENCAPSULAMENTO = "campo_encapsulamento"
CAMPO_SENHA = "campo_senha"

# O que pertence a UMA pesquisa (é apagado em "Nova pesquisa" e "Voltar ao pedido").
_DA_PESQUISA = (TRABALHO, REGISTRO, ESCOLHIDOS, PACOTE, ERRO_AO_GERAR, DEMONSTRACAO, ERRO_DEMONSTRACAO)


def ler(chave: str, padrao=None):
    return st.session_state.get(chave, padrao)


def gravar(chave: str, valor) -> None:
    st.session_state[chave] = valor


def etapa() -> str:
    return ler(ETAPA, "pedido")


def ir_para(nova: str) -> None:
    if nova not in dict(ETAPAS):
        raise ValueError(f"etapa desconhecida: {nova}")
    gravar(ETAPA, nova)


def limpar_pesquisa() -> None:
    """Apaga o que é da pesquisa atual (o pedido digitado fica, para ser ajustado)."""
    for chave in _DA_PESQUISA:
        if chave in st.session_state:
            del st.session_state[chave]


def pesquisas_feitas() -> int:
    return ler(PESQUISAS_FEITAS, 0)


def contar_pesquisa() -> None:
    gravar(PESQUISAS_FEITAS, pesquisas_feitas() + 1)


def escolhidos() -> set[int]:
    return ler(ESCOLHIDOS, set())
