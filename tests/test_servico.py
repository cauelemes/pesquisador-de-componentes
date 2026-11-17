"""Testes da camada de serviço (sem rede, sem créditos).

    python -m unittest tests.test_servico -v
"""

import json
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch

from openpyxl import load_workbook

from backend.compartilhado import together_client as tg
from backend.compartilhado import uso as contador_de_uso
from backend.compartilhado.modelos import PedidoComponente
from backend.pesquisa import tavily_client
from backend.pesquisa.registro import RegistroPesquisa
from backend.servico import pesquisa as servico
from tests import test_agente as ta


def executar(roteiro, **opcoes) -> servico.ExecucaoPesquisa:
    """Roda o SERVIÇO com a LLM e o Tavily simulados. A LLM falsa registra uso no contador, como a real faria."""
    fila = iter(roteiro)

    def falso_conversar(mensagens, **kwargs):
        resposta = next(fila)
        contador_de_uso.registrar_sucesso(kwargs["modelo"], 1000, 200, tokens_em_cache=600, tokens_raciocinio=150)
        return resposta

    with (
        patch.object(tg, "conversar", side_effect=falso_conversar),
        patch.object(tavily_client, "buscar", side_effect=ta.falso_buscar),
        patch.object(tavily_client, "extrair", side_effect=ta.falso_extrair),
    ):
        return servico.executar_pesquisa(ta.PEDIDO, **opcoes)


def execucao_feliz() -> servico.ExecucaoPesquisa:
    return executar([ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS))])


class TestExecutarPesquisa(unittest.TestCase):
    def test_devolve_relatorio_e_registro(self):
        execucao = execucao_feliz()
        self.assertEqual(len(execucao.relatorio.resultado.achados), 3)
        self.assertEqual(execucao.registro.resultado, execucao.relatorio.resultado)
        self.assertGreaterEqual(execucao.registro.fim, execucao.registro.inicio)
        self.assertEqual(execucao.registro.modelo, "zai-org/GLM-5.3-Flash")

    def test_uso_vem_do_contador_ativo_so_durante_a_pesquisa(self):
        uso = execucao_feliz().registro.uso
        self.assertEqual((uso.pedidos, uso.tokens_entrada, uso.tokens_saida), (2, 2000, 400))  # 2 chamadas à LLM
        self.assertEqual((uso.tokens_em_cache, uso.tokens_raciocinio), (1200, 300))
        self.assertGreater(uso.custo_estimado_usd, 0)
        self.assertIsNone(contador_de_uso.coletor_ativo())  # o contador foi desativado ao final

    def test_opcoes_chegam_ao_agente(self):
        with patch.object(servico, "pesquisar", wraps=lambda pedido, **kw: ta.rodar(
                [ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS))], **kw)[0]) as mock:
            execucao = servico.executar_pesquisa(ta.PEDIDO, esforco="low", timeout=30, modelo="outro/m")
        self.assertEqual({k: mock.call_args.kwargs[k] for k in ("esforco", "timeout", "modelo")},
                         {"esforco": "low", "timeout": 30, "modelo": "outro/m"})
        self.assertEqual((execucao.registro.modelo, execucao.registro.esforco), ("outro/m", "low"))


class TestMontarPacote(unittest.TestCase):
    def setUp(self):
        self.registro = execucao_feliz().registro
        self.achados = self.registro.resultado.achados

    def test_zip_tem_exatamente_os_tres_arquivos(self):
        pacote = zipfile.ZipFile(BytesIO(servico.montar_pacote(self.registro, self.achados[:2])))
        self.assertEqual(sorted(pacote.namelist()),
                         sorted(["planilhas/ficha_de_componentes.xlsx", "debug/pesquisa.json", "pedido.json"]))
        livro = load_workbook(BytesIO(pacote.read("planilhas/ficha_de_componentes.xlsx")))
        self.assertEqual(livro["Alternativas"].max_row, 3)  # cabeçalho + os 2 escolhidos
        self.assertEqual(RegistroPesquisa.model_validate_json(pacote.read("debug/pesquisa.json")), self.registro)
        self.assertEqual(PedidoComponente.model_validate(json.loads(pacote.read("pedido.json"))), self.registro.pedido)

    def test_nomes_fixos_mesmo_com_texto_malicioso(self):
        perigoso = self.achados[0].model_copy(update={"fabricante": "../../etc/passwd", "mpn": "/tmp/x"})
        resultado = self.registro.resultado.model_copy(update={"achados": [perigoso]})
        registro = self.registro.model_copy(update={"resultado": resultado})
        pacote = zipfile.ZipFile(BytesIO(servico.montar_pacote(registro, [perigoso])))
        self.assertEqual(set(pacote.namelist()), {servico.ARQUIVO_PLANILHA, servico.ARQUIVO_REGISTRO,
                                                  servico.ARQUIVO_PEDIDO})

    def test_pacote_com_registro_acima_do_limite_da_erro_claro(self):
        from backend.pesquisa import registro as rg

        with patch.object(rg, "MAX_BYTES_DO_REGISTRO", 1000), self.assertRaises(rg.ErroDeRegistro):
            servico.montar_pacote(self.registro, self.achados[:1])

    def test_escolha_invalida_e_recusada(self):
        fora = self.achados[0].model_copy(update={"mpn": "OUTRO123"})  # parecido, mas não é um achado válido
        casos = {
            "nenhum": [],
            "mais de 5": [self.achados[0]] * 6,
            "fora do resultado": [self.achados[0], fora],
            "repetido": [self.achados[0], self.achados[0]],
        }
        for nome, escolhidos in casos.items():
            with self.subTest(nome), self.assertRaises(servico.ErroDeEscolha):
                servico.montar_pacote(self.registro, escolhidos)


if __name__ == "__main__":
    unittest.main()
