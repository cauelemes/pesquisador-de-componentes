"""O registro de UMA pesquisa: um JSON autossuficiente, para depurar e (no futuro) para o modo demonstração.

"Autossuficiente" quer dizer: o arquivo traz o pedido, o resultado, os passos do agente E o texto de cada página lida
(o mesmo que a LLM recebeu). Com ele dá para refazer a conferência sem chamar nenhuma API.

Segurança: um registro pode voltar de fora (um arquivo baixado, enviado, editado à mão). Por isso `carregar_registro`
valida TUDO com Pydantic, com `extra="forbid"` e limites de tamanho: um arquivo adulterado é recusado, não "consertado".
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..compartilhado.modelos import MAX_URL, PedidoComponente, ResultadoPesquisa
from ..compartilhado.versao import VERSAO
from .agente import ESFORCOS_ACEITOS, MAX_CARACTERES_PAGINA, RelatorioPesquisa

# Preços do modelo, em US$ por MILHÃO de tokens. Servem só para uma ESTIMATIVA: confira a tabela de preços da Together
# antes de confiar no valor. Os tokens em cache e de raciocínio são PARTES da entrada e da saída (não somam por fora).
PRECO_ENTRADA_POR_MILHAO = 0.15  # entrada que NÃO veio do cache
PRECO_CACHE_POR_MILHAO = 0.03  # entrada servida do cache (mais barata)
PRECO_SAIDA_POR_MILHAO = 0.50  # saída, inclusive o raciocínio

# Limite TOTAL do registro em JSON. Um registro real tem algumas centenas de KB. Acima disso, gravar ou ler levanta
# `ErroDeRegistro` com uma mensagem clara: NADA é cortado em silêncio (um corte quebraria a reconstrução da conversa).
MAX_BYTES_DO_REGISTRO = 5 * 1024 * 1024
MAX_PASSOS = 100
MAX_PAGINAS = 100
MAX_ERRO_DE_PAGINA = 300


class ErroDeRegistro(ValueError):
    """O registro não pode ser gravado ou lido (ex.: passou do limite de tamanho). Mensagem para o usuário."""

_CONFIG = ConfigDict(str_strip_whitespace=False, extra="forbid")  # textos ficam como vieram: é um retrato fiel
Inteiro = Annotated[int, Field(ge=0)]
Url = Annotated[str, Field(min_length=1, max_length=MAX_URL)]


# =====================================================================================================================
# Partes do registro
# =====================================================================================================================
class FerramentaRegistrada(BaseModel):
    """Uma ferramenta executada num passo: os argumentos COMPLETOS que a LLM mandou e o resultado COMPLETO que ela
    recebeu (o texto das páginas já vem limitado a 12 mil caracteres pelo agente; as buscas, pelo Tavily)."""

    model_config = _CONFIG
    nome: str = Field(max_length=80)
    argumentos: str  # sem limite próprio: o limite é o do arquivo inteiro (MAX_BYTES_DO_REGISTRO)
    resultado: str


class PassoRegistrado(BaseModel):
    """Um passo do ciclo, com as mesmas chaves que `agente._Ciclo._passo` monta."""

    model_config = _CONFIG
    passo: Annotated[int, Field(ge=1)]
    forcado: bool
    cortes_por_tokens: Annotated[int, Field(ge=0, le=2)]
    texto: str  # o texto completo da LLM naquele passo
    tokens_entrada: Inteiro
    tokens_saida: Inteiro
    tokens_raciocinio: Inteiro
    tokens_em_cache: Inteiro
    ferramentas: list[FerramentaRegistrada] = Field(max_length=50)


class OrcamentoRegistrado(BaseModel):
    model_config = _CONFIG
    passos: Inteiro
    buscas: Inteiro
    leituras: Inteiro
    passos_usados: Inteiro
    buscas_usadas: Inteiro
    leituras_usadas: Inteiro


class UsoRegistrado(BaseModel):
    """Tokens e custo da pesquisa, a partir dos contadores de `uso.py` (só o que aconteceu DURANTE esta pesquisa)."""

    model_config = _CONFIG
    pedidos: Inteiro  # chamadas à LLM com uso confirmado
    tokens_entrada: Inteiro  # inclui os em cache
    tokens_saida: Inteiro  # inclui os de raciocínio
    tokens_em_cache: Inteiro
    tokens_raciocinio: Inteiro
    pedidos_sem_confirmacao: Inteiro  # falharam ou vieram sem `usage`: o uso deles é só estimado
    tokens_estimados_nao_confirmados: Inteiro
    creditos_tavily: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    custo_estimado_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)]  # só o confirmado
    custo_com_falhas_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)]  # + a estimativa (teto aproximado)


class RegistroPesquisa(BaseModel):
    """Tudo sobre uma pesquisa. ATENÇÃO: `paginas`, `passos` e boa parte de `resultado` vêm da web ou da LLM: são DADO."""

    model_config = _CONFIG
    versao: str = Field(min_length=1, max_length=40)
    inicio: datetime
    fim: datetime
    duracao_segundos: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    modelo: str = Field(min_length=1, max_length=200)
    esforco: Literal[ESFORCOS_ACEITOS] | None  # o mesmo conjunto que `pesquisar` aceita
    pedido: PedidoComponente
    resultado: ResultadoPesquisa
    orcamento: OrcamentoRegistrado
    uso: UsoRegistrado
    passos: list[PassoRegistrado] = Field(max_length=MAX_PASSOS)
    # {url: texto}: o MESMO texto que a LLM recebeu (e que a conferência usou), cortado a 12 mil caracteres.
    paginas: dict[Url, Annotated[str, Field(max_length=MAX_CARACTERES_PAGINA)]] = Field(max_length=MAX_PAGINAS)
    paginas_com_erro: dict[Url, Annotated[str, Field(max_length=MAX_ERRO_DE_PAGINA)]] = Field(max_length=MAX_PAGINAS)

    @model_validator(mode="after")
    def _datas_coerentes(self) -> "RegistroPesquisa":
        if self.fim < self.inicio:
            raise ValueError("fim antes do início")
        return self


# =====================================================================================================================
# Montar, salvar e carregar
# =====================================================================================================================
def estimar_custo_usd(tokens_entrada: int, tokens_em_cache: int, tokens_saida: int) -> float:
    """Custo estimado em US$. O cache é PARTE da entrada: a entrada "cheia" é o que sobra tirando o cache."""
    sem_cache = max(tokens_entrada - tokens_em_cache, 0)
    custo = (
        sem_cache * PRECO_ENTRADA_POR_MILHAO
        + tokens_em_cache * PRECO_CACHE_POR_MILHAO
        + tokens_saida * PRECO_SAIDA_POR_MILHAO
    ) / 1_000_000
    return round(custo, 6)


def _uso_registrado(resumo: dict | None) -> UsoRegistrado:
    """Converte o `ColetorDeUso.resumo()` no formato do registro. Sem resumo (nenhum contador ativo), tudo zero."""
    resumo = resumo or {}
    tokens = resumo.get("tokens", {})
    nao_confirmado = resumo.get("nao_confirmado", {})
    entrada, saida, cache = tokens.get("entrada", 0), tokens.get("saida", 0), tokens.get("em_cache", 0)
    estimados = nao_confirmado.get("tokens_estimados", 0)
    custo = estimar_custo_usd(entrada, cache, saida)
    return UsoRegistrado(
        pedidos=tokens.get("pedidos", 0),
        tokens_entrada=entrada,
        tokens_saida=saida,
        tokens_em_cache=cache,
        tokens_raciocinio=tokens.get("raciocinio", 0),
        pedidos_sem_confirmacao=nao_confirmado.get("pedidos_com_falha", 0) + nao_confirmado.get("pedidos_sem_uso", 0),
        tokens_estimados_nao_confirmados=estimados,
        creditos_tavily=resumo.get("tavily", {}).get("creditos", 0.0),
        custo_estimado_usd=custo,
        # A estimativa dos pedidos sem confirmação é só de ENTRADA; cobramos pelo preço cheio (sem cache): é um teto.
        custo_com_falhas_usd=round(custo + estimados * PRECO_ENTRADA_POR_MILHAO / 1_000_000, 6),
    )


def montar_registro(
    pedido: PedidoComponente,
    relatorio: RelatorioPesquisa,
    inicio: datetime,
    fim: datetime,
    modelo: str,
    esforco: str | None,
    *,
    uso: dict | None = None,
) -> RegistroPesquisa:
    """Junta tudo num `RegistroPesquisa` (validado: se algo sair do formato, o erro aparece aqui, não ao carregar).

    Args:
        uso: o `ColetorDeUso.resumo()` de um contador ativo SÓ durante esta pesquisa (ver `servico.pesquisa`).
    """
    return RegistroPesquisa(
        versao=VERSAO,
        inicio=inicio,
        fim=fim,
        duracao_segundos=round((fim - inicio).total_seconds(), 3),
        modelo=modelo,
        esforco=esforco,
        pedido=pedido,
        resultado=relatorio.resultado,
        orcamento=OrcamentoRegistrado(**vars(relatorio.orcamento)),
        uso=_uso_registrado(uso),
        passos=relatorio.passos,  # lista de dicts -> o Pydantic converte (e confere) cada um em `PassoRegistrado`
        paginas={url: p.conteudo[:MAX_CARACTERES_PAGINA] for url, p in relatorio.paginas_lidas.items()},
        paginas_com_erro={url: erro[:MAX_ERRO_DE_PAGINA] for url, erro in relatorio.paginas_com_erro.items()},
    )


def registro_em_json(registro: RegistroPesquisa) -> bytes:
    """O registro em JSON (UTF-8, indentado para leitura humana), conferindo o limite total de tamanho.

    Raises:
        ErroDeRegistro: o JSON passou de MAX_BYTES_DO_REGISTRO (nada é cortado).
    """
    conteudo = registro.model_dump_json(indent=2).encode("utf-8")
    if len(conteudo) > MAX_BYTES_DO_REGISTRO:
        raise ErroDeRegistro(
            f"O registro da pesquisa tem {len(conteudo) / 1_048_576:.1f} MB, acima do limite de "
            f"{MAX_BYTES_DO_REGISTRO / 1_048_576:.0f} MB. Nada foi gravado (e nada foi cortado)."
        )
    return conteudo


def salvar_registro(registro: RegistroPesquisa, caminho: Path | str) -> Path:
    """Grava o registro em JSON. Cria a pasta se preciso.

    Raises:
        ErroDeRegistro: o registro passou do limite de tamanho (o arquivo não é criado).
    """
    conteudo = registro_em_json(registro)  # confere o tamanho ANTES de criar qualquer coisa no disco
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(conteudo)
    return caminho


def carregar_registro(caminho: Path | str) -> RegistroPesquisa:
    """Lê e VALIDA um registro. Arquivo vindo de fora é dado, não confiança.

    Raises:
        ErroDeRegistro: arquivo grande demais.
        pydantic.ValidationError: JSON malformado, campo a mais, tipo errado, texto longo demais...
    """
    caminho = Path(caminho)
    if caminho.stat().st_size > MAX_BYTES_DO_REGISTRO:  # confere ANTES de ler tudo para a memória
        raise ErroDeRegistro(f"registro grande demais (máximo {MAX_BYTES_DO_REGISTRO // 1_048_576} MB)")
    return RegistroPesquisa.model_validate_json(caminho.read_bytes())
