"""Camada de serviço da pesquisa: a ÚNICA porta de entrada que o frontend (e o CLI) usam.

Por que existe uma camada a mais: o frontend não precisa saber que há um agente, um contador de uso, um registro e uma
planilha. Ele pede "pesquise isto" e "monte o pacote com estes escolhidos". Se amanhã o agente mudar, só este arquivo
precisa acompanhar.
"""

import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from ..compartilhado import uso as contador_de_uso
from ..compartilhado.modelos import AchadoComponente, PedidoComponente
from ..pesquisa.progresso import EventoDeProgresso
from ..pesquisa.agente import MODELO_AGENTE_PESQUISA, TIMEOUT_CHAMADA_SEGUNDOS, Orcamento, RelatorioPesquisa, pesquisar
from ..pesquisa.registro import RegistroPesquisa, montar_registro, registro_em_json
from ..planilhas.gera_planilha import MAX_ACHADOS_NA_PLANILHA, gerar_planilha

# Nomes FIXOS dentro do .zip. Nunca derivados de texto da web ou do usuário: um nome como "../../.bashrc" poderia,
# ao extrair o zip, escrever fora da pasta escolhida (o ataque "zip slip").
ARQUIVO_PLANILHA = "planilhas/ficha_de_componentes.xlsx"
ARQUIVO_REGISTRO = "debug/pesquisa.json"
ARQUIVO_PEDIDO = "pedido.json"


class ErroDeEscolha(ValueError):
    """A escolha de achados para o pacote é inválida (mensagem escrita para o usuário)."""


@dataclass(frozen=True)
class ExecucaoPesquisa:
    """O que `executar_pesquisa` devolve: o relatório do agente (para a tela) e o registro (para salvar e empacotar)."""

    relatorio: RelatorioPesquisa
    registro: RegistroPesquisa


def _agora() -> datetime:
    """Agora, em UTC e com fuso explícito (datas sem fuso são ambíguas num arquivo que pode ir para outro lugar)."""
    return datetime.now(timezone.utc)


def executar_pesquisa(
    pedido: PedidoComponente,
    cancelar: Callable[[], bool] | None = None,
    orcamento: Orcamento | None = None,
    modelo: str = MODELO_AGENTE_PESQUISA,
    esforco: str | None = None,
    timeout: float = TIMEOUT_CHAMADA_SEGUNDOS,
    ao_evento: Callable[[EventoDeProgresso], None] | None = None,
) -> ExecucaoPesquisa:
    """Roda a pesquisa, mede a duração e o uso, e monta o registro.

    O contador de uso é um `ColetorDeUso` NOVO, ativo só durante esta pesquisa: assim o registro traz o gasto DELA, e
    não o da sessão inteira. (Quando houver frontend com contador da sessão, ele precisará somar este resumo ao dele.)
    """
    coletor = contador_de_uso.ColetorDeUso()
    inicio = _agora()
    with contador_de_uso.usando(coletor):
        relatorio = pesquisar(
            pedido, orcamento=orcamento, cancelar=cancelar, modelo=modelo, esforco=esforco, timeout=timeout,
            ao_evento=ao_evento,  # acompanhamento opcional: o terminal e a tela passam o seu
        )
    fim = _agora()
    registro = montar_registro(pedido, relatorio, inicio, fim, modelo, esforco, uso=coletor.resumo())
    return ExecucaoPesquisa(relatorio=relatorio, registro=registro)


def validar_escolha(registro: RegistroPesquisa, achados_escolhidos: list[AchadoComponente]) -> None:
    """De 1 a 5, sem repetidos, e TODOS entre os achados válidos do resultado.

    A comparação é do achado inteiro (modelos Pydantic comparam campo a campo): um achado "parecido", com uma citação
    trocada ou outra URL, não passa. Isso impede montar uma planilha com algo que a conferência não aprovou.
    """
    if not 1 <= len(achados_escolhidos) <= MAX_ACHADOS_NA_PLANILHA:
        raise ErroDeEscolha(f"Escolha de 1 a {MAX_ACHADOS_NA_PLANILHA} componentes.")
    validos = registro.resultado.achados
    for i, achado in enumerate(achados_escolhidos):
        if achado not in validos:
            raise ErroDeEscolha(f"O componente nº {i + 1} escolhido não está entre os achados válidos da pesquisa.")
        if achado in achados_escolhidos[:i]:
            raise ErroDeEscolha(f"O componente nº {i + 1} foi escolhido mais de uma vez.")


def montar_pacote(registro: RegistroPesquisa, achados_escolhidos: list[AchadoComponente]) -> bytes:
    """Monta o .zip (em memória) com a planilha, o registro e o pedido. Devolve os bytes do zip.

    Raises:
        ErroDeEscolha: a escolha não passa em `validar_escolha`.
        ErroDeRegistro: o registro passou do limite de tamanho.
    """
    validar_escolha(registro, achados_escolhidos)
    resultado = registro.resultado
    planilha = gerar_planilha(
        registro.pedido,
        achados_escolhidos,
        resultado.resumo_conferido,
        resultado.alertas,
        recusados=resultado.recusados,
        resumo_da_llm=resultado.resumo_da_llm,
    )
    memoria = BytesIO()
    with zipfile.ZipFile(memoria, "w", compression=zipfile.ZIP_DEFLATED) as pacote:
        pacote.writestr(ARQUIVO_PLANILHA, planilha)
        pacote.writestr(ARQUIVO_REGISTRO, registro_em_json(registro))  # mesmo limite de tamanho do arquivo salvo
        pacote.writestr(ARQUIVO_PEDIDO, registro.pedido.model_dump_json(indent=2))
    return memoria.getvalue()
