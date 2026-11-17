"""Eventos de progresso: mensagens curtas sobre o que a pesquisa está fazendo AGORA.

Para que servem: uma chamada à LLM com raciocínio pode levar minutos, e sem notícias o usuário acha que travou. O ciclo
do agente emite um `EventoDeProgresso` em cada ponto importante (passo, busca, leitura, registro...), e QUEM QUISER
acompanhar passa um callback (`ao_evento`). Dois consumidores usam o mesmo recurso: o terminal (`testar_agente.py`) e a
tela (`frontend/execucao.py`).

Duas garantias:
- Sem callback, nada muda: `emitir` não faz nada.
- Um callback com defeito NUNCA quebra a pesquisa: a exceção é capturada, vira UM alerta curto, e a pesquisa segue.

ATENÇÃO: consultas, URLs e falas vêm da LLM e da web. São DADO: `limpar_texto` corta e tira quebras de linha e
caracteres de controle, e quem exibe deve mostrar como texto literal (st.text, print).
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

TIPOS = ("passo", "fala_da_llm", "busca", "leitura", "leitura_falhou", "registro", "correcao", "aviso")
MAX_TEXTO_EVENTO = 200
ALERTA_CALLBACK = "o acompanhamento do progresso falhou; a pesquisa seguiu sem ele"

_ESPACOS = re.compile(r"\s+")


@dataclass(frozen=True)
class EventoDeProgresso:
    segundos: float  # tempo desde o início da pesquisa
    tipo: str  # um de TIPOS
    texto: str  # curto (até MAX_TEXTO_EVENTO), numa linha só


def limpar_texto(texto) -> str:
    """Uma linha só, sem caracteres de controle, com no máximo MAX_TEXTO_EVENTO caracteres (cortado com "…").

    Os caracteres de controle (categoria "C" do Unicode: \\n, \\r, \\t, \\x00, \\x1b...) viram espaço: uma URL ou fala
    com "\\n" ou com códigos de terminal não pode quebrar a linha do terminal nem a da tela.
    """
    texto = "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(texto or ""))
    texto = _ESPACOS.sub(" ", texto).strip()
    if len(texto) > MAX_TEXTO_EVENTO:
        texto = texto[: MAX_TEXTO_EVENTO - 1].rstrip() + "…"
    return texto


def emitir(
    callback: Callable[[EventoDeProgresso], None] | None,
    tipo: str,
    texto: str,
    *,
    segundos: float = 0.0,
    alertas: list[str] | None = None,
) -> EventoDeProgresso | None:
    """Monta o evento e entrega ao callback. Devolve o evento (ou None, sem callback).

    Se o callback levantar exceção, ela é engolida de propósito: o acompanhamento é um extra, a pesquisa é o principal.
    O alerta `ALERTA_CALLBACK` entra em `alertas` uma ÚNICA vez (mesmo que o callback falhe em todos os eventos).
    """
    if callback is None:
        return None
    if tipo not in TIPOS:  # erro NOSSO (de programação), não da web: melhor descobrir já
        raise ValueError(f"tipo de evento desconhecido: {tipo}")
    evento = EventoDeProgresso(segundos=round(max(segundos, 0.0), 1), tipo=tipo, texto=limpar_texto(texto))
    try:
        callback(evento)
    except Exception:  # qualquer defeito do callback
        if alertas is not None and ALERTA_CALLBACK not in alertas:
            alertas.append(ALERTA_CALLBACK)
    return evento


def formatar_tempo(segundos: float) -> str:
    """83 -> "01:23" (minutos:segundos); acima de uma hora, os minutos passam de 60 ("75:00")."""
    total = int(max(segundos, 0))
    return f"{total // 60:02d}:{total % 60:02d}"
