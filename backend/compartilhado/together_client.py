"""Cliente mínimo para conversar com LLMs hospedadas na Together AI.

Este módulo é a "porta de entrada" única para os modelos: o agente chama `conversar`
em vez de falar com o SDK da Together diretamente. Assim, trocar de provedor ou
de modelo no futuro mexe em um só lugar.

Documentação da API: https://docs.together.ai/docs/chat-overview
"""

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from together import Together

from . import uso as contador_de_uso  # (não se chama `uso`: o `conversar` tem uma variável local com esse nome)

# Lê o arquivo .env da raiz do projeto (se existir) e copia seus valores para
# as variáveis de ambiente do processo. Variáveis já definidas no shell têm
# prioridade (override=False), o que é o comportamento esperado em produção.
load_dotenv()

# Modelo usado quando nada é informado. É só um ponto de partida: os IDs mudam
# com o tempo, então confira os disponíveis com `listar_modelos_chat()`.
MODELO_PADRAO = "deepseek-ai/DeepSeek-V4.1-Flash"

# Tempo máximo (em segundos) de espera por uma resposta antes de desistir.
TIMEOUT_SEGUNDOS = 60


class ErroTogether(Exception):
    """Erro ao falar com a Together, com mensagem pensada para quem usa o sistema."""


def _criar_cliente(timeout: float | None = None) -> Together:
    """Cria o cliente da Together a partir da chave guardada no ambiente.

    `timeout`: segundos de espera por uma resposta; None usa `TIMEOUT_SEGUNDOS`.
    """
    chave = os.getenv("TOGETHER_API_KEY")
    if not chave:
        # Falhamos cedo e sem expor nada sensível na mensagem.
        raise ErroTogether(
            "A variável TOGETHER_API_KEY não está definida. "
            "Copie .env.example para .env e preencha a sua chave."
        )
    return Together(api_key=chave, timeout=timeout or TIMEOUT_SEGUNDOS)


def _traduzir_erro(erro: Exception) -> ErroTogether:
    """Converte erros do SDK em mensagens claras, sem vazar a chave de API.

    Usamos `getattr` porque erros de rede não têm `status_code`, e assim não
    dependemos dos nomes exatos das classes de exceção do SDK.
    """
    status = getattr(erro, "status_code", None)
    if status == 401:
        return ErroTogether("Chave de API recusada pela Together (401). Confira o valor no .env.")
    if status == 404:
        return ErroTogether("Modelo não encontrado (404). Verifique o ID, que diferencia maiúsculas.")
    if status == 429:
        return ErroTogether("Limite de requisições atingido (429). Tente novamente em instantes.")
    if status == 400:
        # Requisição recusada: o motivo (ex.: um parâmetro que o modelo não aceita) ajuda a
        # diagnosticar. Descreve o PEDIDO, não traz a chave; truncamos por precaução.
        motivo = str(getattr(erro, "message", "") or "")[:300]
        return ErroTogether(f"A Together recusou a requisição (HTTP 400){': ' + motivo if motivo else '.'}")
    if status == 402:
        return ErroTogether("Os créditos da conta da Together acabaram (402). Verifique o saldo da conta.")
    if status == 403:
        return ErroTogether("A Together negou o acesso a este recurso (403). Confira as permissões da chave.")
    if status is not None and status >= 500:
        return ErroTogether(f"O serviço da Together está com problemas (HTTP {status}). Tente novamente em instantes.")
    if status is not None:
        return ErroTogether(f"A Together respondeu com erro HTTP {status}.")
    # Sem status: falha de rede. O nome da classe (nunca o texto do erro) diz se foi tempo esgotado ou conexão.
    nome = type(erro).__name__
    if "Timeout" in nome:
        return ErroTogether("A Together não respondeu a tempo. Tente novamente em instantes.")
    if "Connection" in nome:
        return ErroTogether("Não foi possível conectar à Together. Confira a conexão com a internet.")
    return ErroTogether(f"Não foi possível falar com a Together ({nome}).")


def _registrar_uso(modelo: str, resposta, mensagens: list[dict], ferramentas: list[dict] | None = None) -> None:
    """Soma ao contador da sessão (`uso.py`) o uso que a Together CONFIRMOU na resposta (`usage`). Se a resposta veio sem
    ele, registra como não confirmado, com a estimativa da entrada. Sem contador ativo, não faz nada."""
    informado = getattr(resposta, "usage", None)
    entrada = getattr(informado, "prompt_tokens", None)
    saida = getattr(informado, "completion_tokens", None)
    if type(entrada) is int and type(saida) is int:
        contador_de_uso.registrar_sucesso(
            modelo,
            entrada,
            saida,
            tokens_em_cache=_tokens_detalhados(informado, "prompt_tokens_details", "cached_tokens"),
            tokens_raciocinio=_tokens_detalhados(informado, "completion_tokens_details", "reasoning_tokens"),
        )
    else:
        contador_de_uso.registrar_sem_uso(modelo, contador_de_uso.estimar_tokens_das_mensagens(mensagens, ferramentas=ferramentas))


def _campo(objeto, nome: str):
    """Lê um campo de um objeto do SDK OU de um dicionário (os campos que o SDK não declara chegam como dicionário)."""
    if isinstance(objeto, dict):
        return objeto.get(nome)
    return getattr(objeto, nome, None)


def _tokens_detalhados(uso, grupo: str, nome: str) -> int:
    """Um contador de `usage.<grupo>.<nome>` (ex.: completion_tokens_details.reasoning_tokens), ou 0 se a API não o informar.

    Defensivo de propósito: a Together avisa que o formato do `usage` varia por modelo, e só aceitamos um inteiro.
    """
    valor = _campo(_campo(uso, grupo), nome)
    return valor if type(valor) is int and valor > 0 else 0


@dataclass
class ChamadaFerramenta:
    """Um pedido do modelo: "execute a ferramenta `nome` com estes argumentos"."""

    id: str  # identificador, que o resultado da ferramenta precisa repetir
    nome: str
    argumentos_json: str  # texto JSON, escrito pelo modelo: NÃO confiável, valide antes de usar


@dataclass
class RespostaConversa:
    """Resposta de um turno de conversa, já normalizada (independe do SDK)."""

    texto: str  # vazio quando o modelo só pediu ferramentas
    chamadas: list[ChamadaFerramenta] = field(default_factory=list)
    motivo_parada: str = ""  # "stop", "tool_calls", "length"...
    tokens_entrada: int = 0  # inclui os tokens servidos do cache (`tokens_em_cache` é uma PARTE deste número)
    tokens_saida: int = 0  # inclui os tokens de raciocínio, se o modelo raciocinou
    # Quanto da SAÍDA foi raciocínio (`usage.completion_tokens_details.reasoning_tokens`) e quanto da ENTRADA veio do cache
    # (`usage.prompt_tokens_details.cached_tokens`; custa 5x menos). 0 se a API não informar. Confirmado no GLM-5.3-Flash
    # com `testar_uso_together`; o SDK não declara estes campos, então eles chegam como dicionário (`_tokens_detalhados`).
    tokens_raciocinio: int = 0
    tokens_em_cache: int = 0
    # Texto do raciocínio, se o modelo o devolver. Serve para estudar as decisões do agente;
    # NÃO é reenviado ao modelo (não entra em `como_mensagem`).
    raciocinio: str = ""

    def como_mensagem(self) -> dict:
        """A mensagem do "assistant" para anexar ao histórico (inclui os pedidos de ferramenta)."""
        mensagem: dict = {"role": "assistant", "content": self.texto}
        if self.chamadas:
            mensagem["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.nome, "arguments": c.argumentos_json}}
                for c in self.chamadas
            ]
        return mensagem


def mensagem_resultado(chamada: ChamadaFerramenta, conteudo: str) -> dict:
    """A mensagem com o resultado de uma ferramenta, para devolver ao modelo.

    TODO: formato no padrão OpenAI (role "tool" + tool_call_id); a documentação da Together
          consultada não detalha o formato. Confirmar na primeira execução real.
    """
    return {"role": "tool", "tool_call_id": chamada.id, "content": conteudo}


def conversar(
    mensagens: list[dict],
    *,
    ferramentas: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    modelo: str | None = None,
    temperatura: float = 0.2,
    max_tokens: int = 2048,
    parametros_extras: dict | None = None,
    timeout: float | None = None,
) -> RespostaConversa:
    """Envia uma conversa (lista de mensagens) a um modelo, que pode pedir o uso de ferramentas.

    A conversa é você quem mantém: anexe ao histórico a
    mensagem do modelo (`resposta.como_mensagem()`) e, para cada pedido de ferramenta, o
    resultado (`mensagem_resultado`), e chame de novo.

    Args:
        mensagens: o histórico completo, com papéis "system", "user", "assistant" e "tool".
        ferramentas: definições das ferramentas no formato
            {"type": "function", "function": {"name", "description", "parameters"}}.
        tool_choice: "auto" (o modelo decide), "none" (só texto) ou "required" (precisa usar
            alguma ferramenta); ou um dicionário que FORÇA uma ferramenta específica:
            {"type": "function", "function": {"name": "nome_da_ferramenta"}}. None = padrão da API.
        modelo: ID do modelo. Se None, usa MODELO_PADRAO.
        timeout: segundos de espera pela resposta INTEIRA. Modelos que raciocinam antes de
            responder podem levar bem mais que o padrão (`TIMEOUT_SEGUNDOS`); None usa o padrão.

    Raises:
        ErroTogether: chave ausente/inválida, modelo inexistente, falha de rede etc.
    """
    modelo = modelo or MODELO_PADRAO
    opcoes: dict = {}
    if ferramentas:
        opcoes["tools"] = ferramentas
    if tool_choice:
        opcoes["tool_choice"] = tool_choice

    cliente = _criar_cliente(timeout)
    try:
        resposta = cliente.chat.completions.create(
            model=modelo,
            messages=mensagens,
            temperature=temperatura,
            max_tokens=max_tokens,
            **opcoes,
            **(parametros_extras or {}),
        )
    except Exception as erro:
        contador_de_uso.registrar_falha(modelo, contador_de_uso.estimar_tokens_das_mensagens(mensagens, ferramentas=ferramentas))
        raise _traduzir_erro(erro) from None
    _registrar_uso(modelo, resposta, mensagens, ferramentas)

    escolha = resposta.choices[0]
    mensagem = escolha.message
    chamadas = []
    for chamada in (getattr(mensagem, "tool_calls", None) or []):
        argumentos = chamada.function.arguments
        chamadas.append(
            ChamadaFerramenta(
                id=chamada.id,
                nome=chamada.function.name,
                # Normalmente já é texto; se vier um objeto, voltamos a texto para validar igual.
                argumentos_json=argumentos if isinstance(argumentos, str) else json.dumps(argumentos),
            )
        )
    uso = getattr(resposta, "usage", None)
    return RespostaConversa(
        texto=(mensagem.content or "") if mensagem else "",
        chamadas=chamadas,
        motivo_parada=str(escolha.finish_reason or ""),
        tokens_entrada=getattr(uso, "prompt_tokens", 0) or 0,
        tokens_saida=getattr(uso, "completion_tokens", 0) or 0,
        tokens_raciocinio=_tokens_detalhados(uso, "completion_tokens_details", "reasoning_tokens"),
        tokens_em_cache=_tokens_detalhados(uso, "prompt_tokens_details", "cached_tokens"),
        raciocinio=(getattr(mensagem, "reasoning", None) or getattr(mensagem, "reasoning_content", None) or ""),
    )


def listar_modelos_chat() -> list[str]:
    """Devolve os IDs (ordenados) dos modelos de chat disponíveis para a sua conta."""
    cliente = _criar_cliente()
    try:
        modelos = cliente.models.list()
    except Exception as erro:
        raise _traduzir_erro(erro) from None

    # A lista mistura tipos (chat, imagem, embeddings...); ficamos só com chat.
    return sorted(m.id for m in modelos if str(getattr(m, "type", "")).lower() == "chat")
