"""Contador de uso: tokens da Together e créditos do Tavily.

Por que existe: cada chamada à Together e ao Tavily gasta dinheiro, e o gasto acontece em vários pontos do sistema.
Este módulo soma tudo num lugar só, sem que o código precise passar um contador de mão em mão: quem quer medir ATIVA
um `ColetorDeUso` (`ativar` ou `usando`) e os clientes (`together_client`, `tavily_client`) registram o que gastam.
Sem coletor ativo (scripts de terminal, a maior parte dos testes), os registros não fazem nada.

Os tokens têm DOIS contadores, porque nem sempre a Together diz quanto gastou:
  - PRINCIPAL (`tokens`): só o que a Together CONFIRMOU no campo `usage` da resposta. É exato (é a contagem do próprio
    provedor), mas deixa de fora o que não foi confirmado. Inclui, como PARTES da entrada e da saída, os tokens
    servidos do cache e os gastos em raciocínio.
  - PESSIMISTA (`tokens_com_falhas`): o principal MAIS uma ESTIMATIVA do que não foi confirmado: pedidos que falharam
    (a resposta de um erro não traz o uso, e não sabemos se a Together cobrou) e respostas que vieram sem `usage`. A
    estimativa é só da ENTRADA (a saída de um pedido que falhou é desconhecida), pelo tamanho do que foi enviado:
    `CARACTERES_POR_TOKEN` por token, de propósito pessimista. É um teto aproximado, nunca um número exato.

Os créditos do Tavily são os que a própria resposta informa (`usage.credits`); se a resposta não trouxer, usamos a regra
documentada do Tavily e marcamos como ESTIMADO.

O coletor ativo fica numa `ContextVar` (uma variável "por contexto de execução"): cada thread ou tarefa vê o seu.
Uma thread nova NÃO herda o contexto de quem a criou; para medir o que ela gasta, copie o contexto para ela
(`contextvars.copy_context().run(...)`).
"""

import contextvars
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

CARACTERES_POR_TOKEN = 3.0  # português tem de 3 a 4 caracteres por token; usar 3 superestima de propósito (contador pessimista)
# O que uma imagem enviada ao modelo de visão pesa na entrada: a Together cobra por blocos de imagem e não sabemos o valor
# exato. Só entra na estimativa de um pedido que FALHOU (nos que deram certo, o valor vem confirmado).
# TODO: calibrar com o `usage` real de uma análise de imagem (prompt_tokens de uma chamada com imagem menos o do texto).
TOKENS_ESTIMADOS_POR_IMAGEM = 1500


@dataclass
class UsoDoModelo:
    """O que um modelo gastou enquanto o coletor esteve ativo."""

    chamadas: int = 0  # pedidos CONFIRMADOS (a Together devolveu o uso)
    tokens_entrada: int = 0
    tokens_saida: int = 0
    tokens_em_cache: int = 0  # PARTE da entrada que veio do cache (mais barata); já incluída em `tokens_entrada`
    tokens_raciocinio: int = 0  # PARTE da saída gasta raciocinando; já incluída em `tokens_saida`
    chamadas_com_falha: int = 0  # pedidos que falharam (erro, tempo esgotado...): o uso é desconhecido
    chamadas_sem_uso: int = 0  # pedidos que deram certo mas vieram sem o campo `usage`
    tokens_estimados_nao_confirmados: int = 0  # a estimativa de entrada dos dois tipos acima


class ColetorDeUso:
    """O uso acumulado enquanto ativo. Seguro para várias threads (uma pode registrar enquanto outra lê)."""

    def __init__(self) -> None:
        self._trava = threading.Lock()
        self._por_modelo: dict[str, UsoDoModelo] = {}
        self._tavily = {"confirmados": 0.0, "estimados": 0.0, "buscas": 0, "leituras": 0}

    def _do_modelo(self, modelo: str) -> UsoDoModelo:
        return self._por_modelo.setdefault(modelo, UsoDoModelo())

    def registrar_sucesso(
        self, modelo: str, tokens_entrada: int, tokens_saida: int, *, tokens_em_cache: int = 0, tokens_raciocinio: int = 0
    ) -> None:
        """Um pedido em que a Together CONFIRMOU o uso. Cache e raciocínio são PARTES da entrada e da saída."""
        with self._trava:
            m = self._do_modelo(modelo)
            m.chamadas += 1
            m.tokens_entrada += max(int(tokens_entrada), 0)
            m.tokens_saida += max(int(tokens_saida), 0)
            m.tokens_em_cache += max(int(tokens_em_cache), 0)
            m.tokens_raciocinio += max(int(tokens_raciocinio), 0)

    def registrar_falha(self, modelo: str, tokens_estimados: int) -> None:
        """Um pedido que falhou: o uso é desconhecido, então só entra a estimativa da entrada (contador pessimista)."""
        with self._trava:
            m = self._do_modelo(modelo)
            m.chamadas_com_falha += 1
            m.tokens_estimados_nao_confirmados += max(int(tokens_estimados), 0)

    def registrar_sem_uso(self, modelo: str, tokens_estimados: int) -> None:
        """Um pedido que deu certo mas a resposta não trouxe o `usage`: não dá para confirmar, então só a estimativa."""
        with self._trava:
            m = self._do_modelo(modelo)
            m.chamadas_sem_uso += 1
            m.tokens_estimados_nao_confirmados += max(int(tokens_estimados), 0)

    def registrar_creditos_tavily(self, creditos: float, *, operacao: str, estimado: bool = False) -> None:
        """Créditos gastos numa busca (`operacao="busca"`) ou leitura de páginas (`"leitura"`). `estimado`: o Tavily não
        informou o valor e usamos a regra documentada."""
        with self._trava:
            self._tavily["estimados" if estimado else "confirmados"] += max(float(creditos), 0.0)
            self._tavily["buscas" if operacao == "busca" else "leituras"] += 1

    def vazio(self) -> bool:
        """Se ainda não houve nenhum registro (nem de modelo nem de Tavily)."""
        with self._trava:
            return not self._por_modelo and not self._tavily["buscas"] and not self._tavily["leituras"]

    def resumo(self) -> dict:
        """Uma cópia (JSON puro) do que foi acumulado, com os totais já calculados."""
        with self._trava:
            modelos = {nome: vars(m).copy() for nome, m in self._por_modelo.items()}
            tavily = dict(self._tavily)
        entrada = sum(m["tokens_entrada"] for m in modelos.values())
        saida = sum(m["tokens_saida"] for m in modelos.values())
        estimados = sum(m["tokens_estimados_nao_confirmados"] for m in modelos.values())
        return {
            "tokens": {
                "entrada": entrada, "saida": saida, "total": entrada + saida,
                "em_cache": sum(m["tokens_em_cache"] for m in modelos.values()),  # parte da entrada
                "raciocinio": sum(m["tokens_raciocinio"] for m in modelos.values()),  # parte da saída
                "pedidos": sum(m["chamadas"] for m in modelos.values()),
            },
            "nao_confirmado": {
                "pedidos_com_falha": sum(m["chamadas_com_falha"] for m in modelos.values()),
                "pedidos_sem_uso": sum(m["chamadas_sem_uso"] for m in modelos.values()),
                "tokens_estimados": estimados,
            },
            "tokens_com_falhas": entrada + saida + estimados,
            "tavily": {
                "creditos": round(tavily["confirmados"] + tavily["estimados"], 4),
                "creditos_confirmados": round(tavily["confirmados"], 4),
                "creditos_estimados": round(tavily["estimados"], 4),
                "buscas": tavily["buscas"], "leituras": tavily["leituras"],
            },
            "por_modelo": modelos,
        }


# ---------------------------------------------------------------- o coletor da sessão atual (uma ContextVar)
_coletor_atual: contextvars.ContextVar[ColetorDeUso | None] = contextvars.ContextVar("coletor_de_uso", default=None)


def ativar(coletor: ColetorDeUso | None) -> None:
    """Faz `coletor` receber os registros feitos a partir de agora, nesta thread (e nas que copiarem o contexto)."""
    _coletor_atual.set(coletor)


@contextmanager
def usando(coletor: ColetorDeUso) -> Iterator[ColetorDeUso]:
    """Ativa `coletor` só dentro do bloco `with` (usado nos testes)."""
    marca = _coletor_atual.set(coletor)
    try:
        yield coletor
    finally:
        _coletor_atual.reset(marca)


def coletor_ativo() -> ColetorDeUso | None:
    return _coletor_atual.get()


def registrar_sucesso(
    modelo: str, tokens_entrada: int, tokens_saida: int, *, tokens_em_cache: int = 0, tokens_raciocinio: int = 0
) -> None:
    if (coletor := coletor_ativo()) is not None:
        coletor.registrar_sucesso(
            modelo, tokens_entrada, tokens_saida, tokens_em_cache=tokens_em_cache, tokens_raciocinio=tokens_raciocinio
        )


def registrar_falha(modelo: str, tokens_estimados: int) -> None:
    if (coletor := coletor_ativo()) is not None:
        coletor.registrar_falha(modelo, tokens_estimados)


def registrar_sem_uso(modelo: str, tokens_estimados: int) -> None:
    if (coletor := coletor_ativo()) is not None:
        coletor.registrar_sem_uso(modelo, tokens_estimados)


def registrar_creditos_tavily(creditos: float, *, operacao: str, estimado: bool = False) -> None:
    if (coletor := coletor_ativo()) is not None:
        coletor.registrar_creditos_tavily(creditos, operacao=operacao, estimado=estimado)


# ---------------------------------------------------------------- estimativa de tokens (só para o que não foi confirmado)
def estimar_tokens_do_texto(texto: str) -> int:
    """Uma estimativa PESSIMISTA de quantos tokens tem o texto (`CARACTERES_POR_TOKEN`). Não é a contagem do modelo."""
    return math.ceil(len(texto) / CARACTERES_POR_TOKEN) if texto else 0


def estimar_tokens_das_mensagens(mensagens: list[dict], *, ferramentas: list[dict] | None = None) -> int:
    """A estimativa de entrada de um pedido: o texto de cada mensagem, os argumentos de ferramentas já pedidos, as
    definições das ferramentas e um valor fixo por imagem. Só para pedidos que falharam ou vieram sem `usage`."""
    total = 0
    for mensagem in mensagens:
        conteudo = mensagem.get("content")
        if isinstance(conteudo, str):
            total += estimar_tokens_do_texto(conteudo)
        elif isinstance(conteudo, list):
            for bloco in conteudo:
                if not isinstance(bloco, dict):
                    continue
                if bloco.get("type") == "text":
                    total += estimar_tokens_do_texto(str(bloco.get("text") or ""))
                elif bloco.get("type") == "image_url":
                    total += TOKENS_ESTIMADOS_POR_IMAGEM
        for chamada in mensagem.get("tool_calls") or []:
            argumentos = (chamada.get("function") or {}).get("arguments") if isinstance(chamada, dict) else None
            total += estimar_tokens_do_texto(str(argumentos or ""))
    if ferramentas:
        total += estimar_tokens_do_texto(str(ferramentas))
    return total
