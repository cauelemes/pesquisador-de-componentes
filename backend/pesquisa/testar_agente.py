"""Roda o agente de pesquisa pelo terminal. ATENÇÃO: chama as APIs de verdade e GASTA CRÉDITOS (Together e Tavily).

    python -m backend.pesquisa.testar_agente exemplos/pedido_regulador.json
    python -m backend.pesquisa.testar_agente exemplos/pedido_regulador.json --max-passos 5 --salvar tmp_debugs/r.json
    python -m backend.pesquisa.testar_agente exemplos/pedido_regulador.json --modelo outro/modelo --esforco low

`-m` roda o arquivo como módulo do pacote `backend`, o que faz os imports relativos (`from ..`) funcionarem.

Durante a pesquisa, cada evento vira uma linha ("[01:23] busca: ..."), e um "batimento" avisa quando a LLM demora.
Use --silencioso para ver só o resumo final.
"""

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from ..compartilhado.modelos import ROTULO_RESUMO_DA_LLM, PedidoComponente
from ..servico.pesquisa import executar_pesquisa
from .agente import ESFORCOS_ACEITOS, MODELO_AGENTE_PESQUISA, TIMEOUT_CHAMADA_SEGUNDOS, Orcamento
from .progresso import EventoDeProgresso, formatar_tempo
from .registro import ErroDeRegistro, salvar_registro


# Rótulos FIXOS de cada tipo de evento no terminal.
ROTULOS_TERMINAL = {
    "passo": "passo", "fala_da_llm": "LLM", "busca": "busca", "leitura": "leitura",
    "leitura_falhou": "leitura falhou", "registro": "registro", "correcao": "correção", "aviso": "aviso",
}
INTERVALO_DO_BATIMENTO = 30.0  # segundos sem eventos até o primeiro "ainda aguardando"


class AcompanhamentoNoTerminal:
    """Imprime os eventos da pesquisa e um "batimento" quando nada acontece por um tempo.

    Por que o batimento: a chamada à LLM é BLOQUEANTE (o agente fica parado esperando) e, com raciocínio, pode levar
    minutos. É aí que parece travado. Uma thread auxiliar olha o relógio a cada segundo e, a cada 30 s de silêncio,
    imprime uma linha dizendo que ainda está esperando.

    `relogio`, `saida` e `intervalo` são injetáveis: os testes simulam o tempo sem esperar de verdade.
    """

    def __init__(self, saida: TextIO | None = None, relogio=time.monotonic, intervalo: float = INTERVALO_DO_BATIMENTO):
        self.saida = saida or sys.stdout
        self.relogio = relogio
        self.intervalo = intervalo
        self.inicio = self.ultimo_evento = relogio()
        self.proximo_aviso = intervalo  # silêncio (em s) que dispara o próximo "ainda aguardando"
        self.trava = threading.Lock()  # o callback (thread da pesquisa) e o batimento (outra thread) mexem nos mesmos campos
        self.parar = threading.Event()
        self.thread: threading.Thread | None = None

    def _imprimir(self, linha: str) -> None:
        print(linha, file=self.saida, flush=True)  # flush: a linha aparece NA HORA, não quando o buffer encher

    def __call__(self, evento: EventoDeProgresso) -> None:
        """O callback `ao_evento`: uma linha por evento. O texto já vem limpo (uma linha, sem controle, cortado)."""
        with self.trava:
            self.ultimo_evento, self.proximo_aviso = self.relogio(), self.intervalo
            self._imprimir(f"[{formatar_tempo(evento.segundos)}] {ROTULOS_TERMINAL.get(evento.tipo, evento.tipo)}: "
                           f"{evento.texto}")

    def verificar_silencio(self) -> None:
        """Um "tique" do batimento: se o silêncio passou do limite, imprime e marca o próximo limite (+30 s)."""
        with self.trava:
            agora = self.relogio()
            silencio = agora - self.ultimo_evento
            if silencio >= self.proximo_aviso:
                self._imprimir(f"[{formatar_tempo(agora - self.inicio)}] ainda aguardando a LLM ({int(silencio)} s sem "
                               "novidades; chamadas com raciocínio podem levar alguns minutos)")
                self.proximo_aviso += self.intervalo

    def iniciar_batimento(self, a_cada: float = 1.0) -> None:
        def laco():
            while not self.parar.wait(a_cada):  # `wait` dorme até `a_cada` s, mas acorda NA HORA se `parar` for ligado
                self.verificar_silencio()

        self.thread = threading.Thread(target=laco, daemon=True, name="batimento")
        self.thread.start()

    def parar_batimento(self) -> None:
        self.parar.set()
        if self.thread is not None:
            self.thread.join(timeout=5)


def imprimir_resumo(relatorio, registro, modelo: str, esforco: str | None) -> None:
    """O resumo final no terminal (o mesmo para a pesquisa real e para a demonstração)."""
    resultado = relatorio.resultado
    print(f"\nModelo: {modelo} · esforço: {esforco or 'padrão do modelo'}")
    print(relatorio.orcamento.texto())
    print(f"\nAchados válidos: {len(resultado.achados)}")
    for achado in resultado.achados:
        print(f"  - {achado.fabricante} {achado.mpn} ({achado.ciclo_de_vida})\n    {achado.url}")
        for evidencia in achado.evidencias:
            print(f"      [{evidencia.situacao}] {evidencia.requisito}: {evidencia.citacao!r}")
    if resultado.alertas:
        print("\nAlertas:")
        for alerta in resultado.alertas:
            print(f"  - {alerta}")
    print(f"\nResumo conferido: {resultado.resumo_conferido}")
    if resultado.resumo_da_llm:
        print(f"\n{ROTULO_RESUMO_DA_LLM}: {resultado.resumo_da_llm}")
    uso = registro.uso
    print(
        f"\nUso: {uso.tokens_entrada} tokens de entrada ({uso.tokens_em_cache} em cache), {uso.tokens_saida} de saída "
        f"({uso.tokens_raciocinio} de raciocínio) · custo estimado US$ {uso.custo_estimado_usd:.4f} · "
        f"{registro.duracao_segundos:.0f} s"
    )


def main(argumentos: list[str] | None = None) -> int:
    """Código de saída: 0 = deu certo, 1 = erro no pedido, 2 = nenhum achado válido, 3 = registro não salvo."""
    leitor = argparse.ArgumentParser(description="Pesquisa componentes eletrônicos para um pedido em JSON.")
    leitor.add_argument("pedido", type=Path, help="arquivo JSON com o pedido (veja exemplos/)")
    leitor.add_argument("--max-passos", type=int, default=Orcamento.passos, help="limite de chamadas à LLM")
    leitor.add_argument("--salvar", type=Path, help="grava o registro completo (passos, páginas, uso) neste JSON")
    leitor.add_argument("--modelo", default=MODELO_AGENTE_PESQUISA, help="ID do modelo na Together")
    # `choices`: o argparse recusa sozinho qualquer outro valor (ex.: "medium", que o Flash não aceita).
    leitor.add_argument("--esforco", choices=ESFORCOS_ACEITOS, help="esforço de raciocínio (padrão: o do modelo)")
    leitor.add_argument("--silencioso", action="store_true", help="não mostra a atividade, só o resumo final")
    leitor.add_argument(
        "--timeout", type=float, default=TIMEOUT_CHAMADA_SEGUNDOS, help="segundos de espera por chamada à LLM"
    )
    opcoes = leitor.parse_args(argumentos)

    if opcoes.max_passos < 1:
        leitor.error("--max-passos precisa ser pelo menos 1")
    if opcoes.timeout <= 0:
        leitor.error("--timeout precisa ser maior que zero")
    try:
        # O arquivo é entrada externa: passa pelo Pydantic, que recusa campos a mais e valores fora do lugar.
        pedido = PedidoComponente.model_validate_json(opcoes.pedido.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as erro:
        print(f"Pedido inválido: {erro}", file=sys.stderr)
        return 1

    # O CLI usa a MESMA porta de entrada que o frontend usará (a camada de serviço).
    acompanhamento = None if opcoes.silencioso else AcompanhamentoNoTerminal()
    if acompanhamento is not None:
        acompanhamento.iniciar_batimento()
    try:
        execucao = executar_pesquisa(
            pedido, orcamento=Orcamento(passos=opcoes.max_passos), modelo=opcoes.modelo, esforco=opcoes.esforco,
            timeout=opcoes.timeout, ao_evento=acompanhamento,
        )
    finally:
        if acompanhamento is not None:
            acompanhamento.parar_batimento()  # o batimento para quando a pesquisa termina (até com erro)
    relatorio, registro = execucao.relatorio, execucao.registro
    resultado = relatorio.resultado
    imprimir_resumo(relatorio, registro, opcoes.modelo, opcoes.esforco)

    if opcoes.salvar:
        try:
            salvar_registro(registro, opcoes.salvar)
        except ErroDeRegistro as erro:
            print(f"\nRelatório NÃO salvo: {erro}", file=sys.stderr)
            return 3
        print(f"\nRelatório salvo em {opcoes.salvar}")
    return 0 if resultado.achados else 2


if __name__ == "__main__":
    sys.exit(main())
