"""Modo demonstração: repete uma execução REAL gravada, sem chaves e sem rede.

    python -m backend.pesquisa.demo                       # usa exemplos/execucao_gravada.json
    python -m backend.pesquisa.demo tmp_debugs/gravada.json

Como funciona: só três peças são trocadas, as que falam com o mundo (a LLM, a busca e a leitura). Elas devolvem, em
ordem, o que a gravação registrou. TODO o resto é o código real: o ciclo do agente, a conferência, o registro, a
planilha e o pacote. Por isso a demonstração prova o sistema, e não um "teatro" dele.

Se o ciclo pedir algo que a gravação não tem (outra consulta, outra URL, um passo a mais), o replay PARA com
`ErroDeReplay`, dizendo o quê: inventar uma resposta esconderia uma divergência entre o código e a gravação.
"""

import argparse
import json
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from ..compartilhado import together_client as tg
from ..compartilhado.modelos import PaginaLida, ResultadoBusca
from ..servico import pesquisa as servico
from .agente import PREFIXO_FALHA_LLM, Orcamento
from .conferencia import normalizar_texto
from .registro import ErroDeRegistro, RegistroPesquisa, carregar_registro
from .tavily_client import ErroTavily
from .urls import normalizar_url

RAIZ = Path(__file__).resolve().parents[2]
GRAVACAO_PADRAO = RAIZ / "exemplos" / "execucao_gravada.json"
AVISO_DEMONSTRACAO = "DEMONSTRAÇÃO: repetição de uma execução gravada; nenhuma API foi chamada"
_PREFIXO_ERRO_DE_FERRAMENTA = "Erro na ferramenta: "  # como o agente devolve à LLM um erro do Tavily


class ErroDeReplay(RuntimeError):
    """A gravação não tem o que o ciclo pediu (ou não pode ser repetida). A mensagem diz o quê."""


def carregar_gravacao(caminho: Path | str = GRAVACAO_PADRAO) -> RegistroPesquisa:
    """Lê e valida a gravação (é um arquivo: passa pelo Pydantic, como qualquer registro vindo de fora)."""
    caminho = Path(caminho)
    if not caminho.exists():
        raise ErroDeReplay(
            f"Gravação não encontrada: {caminho}. Gere com: python -m backend.pesquisa.sanitizar_registro "
            "tmp_debugs/gravada.json exemplos/execucao_gravada.json"
        )
    return carregar_registro(caminho)


def orcamento_da_gravacao(registro: RegistroPesquisa) -> Orcamento:
    """Os MESMOS limites da execução gravada: o passo forçado, se houve, acontece no mesmo ponto."""
    o = registro.orcamento
    return Orcamento(passos=o.passos, buscas=o.buscas, leituras=o.leituras)


class Replay:
    """As três peças falsas (LLM, busca, leitura), montadas a partir de um registro gravado."""

    def __init__(self, registro: RegistroPesquisa, *, atraso: float = 0.0, dormir: Callable[[float], None] = time.sleep):
        if any(a.startswith("pesquisa cancelada") for a in registro.resultado.alertas):
            raise ErroDeReplay("A gravação foi CANCELADA no meio: ela não pode ser repetida do começo ao fim.")
        self.atraso, self.dormir = atraso, dormir
        self.respostas: deque[tuple[bool, tg.RespostaConversa]] = deque()  # (era um passo forçado?, resposta)
        self.buscas: dict[str, deque] = {}  # consulta normalizada -> resultados gravados (ou erro), na ordem
        self.erros_de_leitura: dict[str, str] = {}  # url normalizada -> erro do Tavily (exceção, não página com erro)
        for passo in registro.passos:
            self._gravar_passo(passo)
        self.paginas = {normalizar_url(u): texto for u, texto in registro.paginas.items()}
        self.paginas_com_erro = {normalizar_url(u): erro for u, erro in registro.paginas_com_erro.items()}
        # Se a gravação terminou por falha da LLM (ex.: tempo esgotado), o replay termina com a MESMA falha.
        self.falha_final = next((a.split(": ", 1)[1] for a in registro.resultado.alertas
                                 if a.startswith(PREFIXO_FALHA_LLM + ": ")), None)

    def _gravar_passo(self, passo) -> None:
        cortada = tg.RespostaConversa(texto="", motivo_parada="length")  # o conteúdo de uma cortada é descartado
        for _ in range(min(passo.cortes_por_tokens, 2)):
            self.respostas.append((passo.forcado, cortada))
        chamadas = [tg.ChamadaFerramenta(f"replay-{passo.passo}-{i}", f.nome, f.argumentos)
                    for i, f in enumerate(passo.ferramentas)]
        if passo.cortes_por_tokens < 2:  # com 2 cortes, o agente descartou o passo: não houve resposta boa
            motivo = "tool_calls" if chamadas else "stop"
            self.respostas.append((passo.forcado, tg.RespostaConversa(texto=passo.texto, chamadas=chamadas,
                                                                      motivo_parada=motivo)))
        for f in passo.ferramentas:
            self._gravar_ferramenta(f.nome, f.argumentos, f.resultado)

    def _gravar_ferramenta(self, nome: str, argumentos: str, resultado: str) -> None:
        try:
            args = json.loads(argumentos)
        except ValueError:
            return  # argumentos inválidos: o agente nem chamou o Tavily
        if nome == "buscar_na_web" and isinstance(args, dict) and isinstance(args.get("consulta"), str):
            if resultado.startswith(_PREFIXO_ERRO_DE_FERRAMENTA):
                gravado = ErroTavily(resultado.removeprefix(_PREFIXO_ERRO_DE_FERRAMENTA))
            elif resultado.startswith("{"):
                gravado = json.loads(resultado).get("resultados", [])
            else:
                return  # "Recusado: ..." (repetida, sem orçamento): o agente não chamou o Tavily
            self.buscas.setdefault(normalizar_texto(args["consulta"]), deque()).append(gravado)
        elif nome == "ler_pagina" and isinstance(args, dict) and resultado.startswith(_PREFIXO_ERRO_DE_FERRAMENTA):
            self.erros_de_leitura[normalizar_url(str(args.get("url", "")))] = resultado.removeprefix(
                _PREFIXO_ERRO_DE_FERRAMENTA)

    # -------------------------------------------------------------------------------- as três peças falsas
    def chamar_llm(self, mensagens: list[dict], *, tool_choice=None, **opcoes) -> tg.RespostaConversa:
        """No lugar de `tg.conversar`: devolve a próxima resposta gravada."""
        if self.atraso:
            self.dormir(self.atraso)  # só para a tela parecer viva; não muda nada no resultado
        if not self.respostas:
            if self.falha_final is not None:
                raise tg.ErroTogether(self.falha_final)
            raise ErroDeReplay("A gravação acabou, mas o agente pediu mais um passo à LLM (o código mudou desde a "
                               "gravação?). Grave uma execução nova.")
        forcado, resposta = self.respostas.popleft()
        if forcado != isinstance(tool_choice, dict):  # o passo forçado precisa cair no mesmo ponto da gravação
            raise ErroDeReplay("Divergência: o agente " + ("NÃO " if forcado else "") + "forçou o registro num passo "
                               "em que a gravação " + ("forçou" if forcado else "não forçou") + ".")
        return resposta

    def buscar(self, consulta: str, **opcoes) -> list[ResultadoBusca]:
        """No lugar de `tavily_client.buscar`: os resultados gravados para ESTA consulta."""
        fila = self.buscas.get(normalizar_texto(consulta))
        if not fila:
            raise ErroDeReplay(f"A gravação não tem a busca {consulta!r}.")
        gravado = fila.popleft()
        if isinstance(gravado, ErroTavily):
            raise gravado
        return [ResultadoBusca(titulo=r.get("titulo", ""), url=r["url"], conteudo=r.get("trecho", "")) for r in gravado]

    def ler(self, urls: list[str], *, foco: str = "", **opcoes) -> list[PaginaLida]:
        """No lugar de `tavily_client.extrair`: o texto gravado de cada página (ou o erro gravado)."""
        paginas = []
        for url in urls:
            chave = normalizar_url(url)
            if chave in self.erros_de_leitura:
                raise ErroTavily(self.erros_de_leitura[chave])
            if chave in self.paginas:
                paginas.append(PaginaLida(url=url, conteudo=self.paginas[chave]))
            elif chave in self.paginas_com_erro:
                paginas.append(PaginaLida(url=url, erro=self.paginas_com_erro[chave]))
            else:
                raise ErroDeReplay(f"A gravação não tem a página {url!r}.")
        return paginas


def repetir(
    registro: RegistroPesquisa,
    *,
    atraso: float = 0.0,
    cancelar: Callable[[], bool] | None = None,
    orcamento: Orcamento | None = None,
    ao_evento: Callable | None = None,
    dormir: Callable[[float], None] = time.sleep,
) -> servico.ExecucaoPesquisa:
    """Repete a gravação pelo caminho REAL (serviço -> agente -> conferência -> registro). O registro sai marcado
    `demonstracao=True`. `atraso`: segundos de espera antes de cada "resposta da LLM" (0 = instantâneo)."""
    replay = Replay(registro, atraso=atraso, dormir=dormir)
    return servico.executar_pesquisa(
        registro.pedido,
        cancelar=cancelar,
        orcamento=orcamento or orcamento_da_gravacao(registro),
        modelo=registro.modelo,
        esforco=registro.esforco,
        ao_evento=ao_evento,
        chamar_llm=replay.chamar_llm,
        buscar=replay.buscar,
        ler=replay.ler,
        demonstracao=True,
    )


def main(argumentos: list[str] | None = None) -> int:
    from .testar_agente import AcompanhamentoNoTerminal, imprimir_resumo  # aqui: evita import circular no carregamento

    leitor = argparse.ArgumentParser(description="Repete uma execução gravada (sem chaves, sem rede).")
    leitor.add_argument("arquivo", nargs="?", type=Path, default=GRAVACAO_PADRAO, help="registro gravado (JSON)")
    leitor.add_argument("--silencioso", action="store_true", help="não mostra a atividade, só o resumo final")
    opcoes = leitor.parse_args(argumentos)

    print(AVISO_DEMONSTRACAO, flush=True)  # SEMPRE a primeira linha
    try:
        gravacao = carregar_gravacao(opcoes.arquivo)
        execucao = repetir(gravacao, ao_evento=None if opcoes.silencioso else AcompanhamentoNoTerminal())
    except (ErroDeReplay, ErroDeRegistro, ValidationError) as erro:  # gravação ausente, adulterada ou incompatível
        print(f"Não foi possível repetir a gravação: {erro}", file=sys.stderr)
        return 1
    imprimir_resumo(execucao.relatorio, execucao.registro, gravacao.modelo, gravacao.esforco)
    return 0 if execucao.registro.resultado.achados else 2


if __name__ == "__main__":
    sys.exit(main())
