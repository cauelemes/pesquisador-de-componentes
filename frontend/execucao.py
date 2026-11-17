"""Executa a pesquisa numa thread, para a tela continuar respondendo (barra de progresso, botão "Cancelar").

REGRA IMPORTANTE: a thread NUNCA toca em `st.*` nem em `st.session_state`. O Streamlit só funciona dentro do
"contexto" de uma execução do script; numa thread comum, essas chamadas falham ou se perdem. Por isso:
- este módulo nem importa `streamlit`;
- a thread escreve num objeto comum (`Trabalho`), que a tela guarda no `session_state` e LÊ a cada atualização;
- o cancelamento é um `threading.Event`: a tela chama `.set()`, e o agente consulta `.is_set()` a cada passo.

O progresso vem de dois lugares, os dois escritos pela thread e só LIDOS pela tela:
- o próprio `Orcamento`: o agente atualiza os contadores DESSE objeto no lugar (passos, buscas e leituras usados);
- a lista de eventos (`registrar_evento`): mensagens curtas do que está acontecendo, protegidas por um `Lock`, porque
  a thread acrescenta enquanto a tela lê (uma lista comum, sem trava, poderia ser lida no meio de uma mudança).
"""

import os
import threading
import time
import traceback
from dataclasses import dataclass, field

from backend.compartilhado.modelos import PedidoComponente
from backend.compartilhado.together_client import ErroTogether
from backend.pesquisa.agente import PREFIXO_FALHA_LLM, Orcamento
from backend.pesquisa.progresso import EventoDeProgresso
from backend.pesquisa.tavily_client import ErroTavily
from backend.servico import pesquisa as servico  # módulo (e não a função): os testes trocam `executar_pesquisa`

CHAVES_DE_API = ("TOGETHER_API_KEY", "TAVILY_API_KEY")
MAX_EVENTOS_GUARDADOS = 50  # a tela mostra só os últimos; guardar todos seria memória à toa


def chaves_faltando() -> list[str]:
    """As variáveis de ambiente de API que não estão definidas (os clientes já leram o `.env` ao serem importados)."""
    return [nome for nome in CHAVES_DE_API if not os.getenv(nome)]


@dataclass
class Trabalho:
    """Uma pesquisa em andamento. A thread ESCREVE `execucao`/`erro`; a tela só LÊ."""

    pedido: PedidoComponente
    orcamento: Orcamento = field(default_factory=Orcamento)
    cancelamento: threading.Event = field(default_factory=threading.Event)
    terminou: threading.Event = field(default_factory=threading.Event)
    execucao: servico.ExecucaoPesquisa | None = None
    erro: str | None = None  # mensagem para o usuário (nunca o texto cru de uma exceção desconhecida)
    thread: threading.Thread | None = None
    inicio: float = field(default_factory=time.monotonic)  # para o "Tempo decorrido" da tela
    eventos: list[EventoDeProgresso] = field(default_factory=list)
    trava: threading.Lock = field(default_factory=threading.Lock)

    def registrar_evento(self, evento: EventoDeProgresso) -> None:
        """O callback `ao_evento`. Roda na THREAD da pesquisa: só mexe nesta lista, com a trava."""
        with self.trava:
            self.eventos.append(evento)
            del self.eventos[:-MAX_EVENTOS_GUARDADOS]  # mantém só os mais recentes

    def ultimos_eventos(self, quantos: int) -> list[EventoDeProgresso]:
        """Uma CÓPIA dos últimos `quantos`, do mais novo para o mais antigo (a tela lê sem segurar a trava)."""
        with self.trava:
            return list(reversed(self.eventos[-quantos:]))

    @property
    def em_andamento(self) -> bool:
        return not self.terminou.is_set()

    @property
    def interrompida(self) -> bool:
        """Cancelada pelo usuário ou parada por falha da LLM (tempo esgotado, chave recusada...)."""
        if self.cancelamento.is_set():
            return True
        alertas = self.execucao.registro.resultado.alertas if self.execucao else []
        return any(alerta.startswith(PREFIXO_FALHA_LLM) for alerta in alertas)


def _trabalhar(trabalho: Trabalho) -> None:
    """O que roda DENTRO da thread. Só mexe no `trabalho` (nada de Streamlit)."""
    try:
        trabalho.execucao = servico.executar_pesquisa(
            trabalho.pedido, cancelar=trabalho.cancelamento.is_set, orcamento=trabalho.orcamento,
            ao_evento=trabalho.registrar_evento,
        )
    except (ErroTogether, ErroTavily) as erro:  # mensagens já escritas para o usuário
        trabalho.erro = str(erro)
    except Exception as erro:  # qualquer outra coisa: só o TIPO vai para a tela; o detalhe vai para o terminal
        traceback.print_exc()
        trabalho.erro = f"Erro inesperado na pesquisa ({type(erro).__name__}). Veja o terminal do servidor."
    finally:
        trabalho.terminou.set()  # sempre: a tela precisa saber que acabou, deu certo ou não


def iniciar(pedido: PedidoComponente) -> Trabalho:
    """Cria o `Trabalho` e dispara a thread. `daemon=True`: a thread não impede o servidor de encerrar."""
    trabalho = Trabalho(pedido=pedido)
    trabalho.thread = threading.Thread(target=_trabalhar, args=(trabalho,), daemon=True, name="pesquisa")
    trabalho.thread.start()
    return trabalho
