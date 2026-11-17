"""Roda o agente de pesquisa pelo terminal. ATENÇÃO: chama as APIs de verdade e GASTA CRÉDITOS (Together e Tavily).

    python -m backend.pesquisa.testar_agente exemplos/pedido_regulador.json
    python -m backend.pesquisa.testar_agente exemplos/pedido_regulador.json --max-passos 5 --salvar tmp_debugs/r.json
    python -m backend.pesquisa.testar_agente exemplos/pedido_regulador.json --modelo outro/modelo --esforco low

`-m` roda o arquivo como módulo do pacote `backend`, o que faz os imports relativos (`from ..`) funcionarem.
"""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from ..compartilhado.modelos import ROTULO_RESUMO_DA_LLM, PedidoComponente
from ..servico.pesquisa import executar_pesquisa
from .agente import ESFORCOS_ACEITOS, MODELO_AGENTE_PESQUISA, TIMEOUT_CHAMADA_SEGUNDOS, Orcamento
from .registro import salvar_registro


def main(argumentos: list[str] | None = None) -> int:
    """Devolve o código de saída do programa: 0 = deu certo, 1 = erro no pedido, 2 = nenhum achado válido."""
    leitor = argparse.ArgumentParser(description="Pesquisa componentes eletrônicos para um pedido em JSON.")
    leitor.add_argument("pedido", type=Path, help="arquivo JSON com o pedido (veja exemplos/)")
    leitor.add_argument("--max-passos", type=int, default=Orcamento.passos, help="limite de chamadas à LLM")
    leitor.add_argument("--salvar", type=Path, help="grava o registro completo (passos, páginas, uso) neste JSON")
    leitor.add_argument("--modelo", default=MODELO_AGENTE_PESQUISA, help="ID do modelo na Together")
    # `choices`: o argparse recusa sozinho qualquer outro valor (ex.: "medium", que o Flash não aceita).
    leitor.add_argument("--esforco", choices=ESFORCOS_ACEITOS, help="esforço de raciocínio (padrão: o do modelo)")
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
    execucao = executar_pesquisa(
        pedido, orcamento=Orcamento(passos=opcoes.max_passos), modelo=opcoes.modelo, esforco=opcoes.esforco,
        timeout=opcoes.timeout,
    )
    relatorio, registro = execucao.relatorio, execucao.registro
    resultado = relatorio.resultado

    print(f"\nModelo: {opcoes.modelo} · esforço: {opcoes.esforco or 'padrão do modelo'}")
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

    if opcoes.salvar:
        salvar_registro(registro, opcoes.salvar)
        print(f"\nRelatório salvo em {opcoes.salvar}")
    return 0 if resultado.achados else 2


if __name__ == "__main__":
    sys.exit(main())
