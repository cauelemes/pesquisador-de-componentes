"""Testes do registro da pesquisa (sem rede, sem créditos).

    python -m unittest tests.test_registro -v
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from backend.compartilhado import together_client as tg
from backend.compartilhado.modelos import PaginaLida
from backend.pesquisa import agente as ag
from backend.pesquisa import registro as rg
from backend.pesquisa.conferencia import conferir_achados
from tests import test_agente as ta  # roteiro, páginas e Tavily simulados

RAIZ = Path(__file__).resolve().parent.parent
INICIO = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
FIM = INICIO + timedelta(seconds=95.5)
# Um `ColetorDeUso.resumo()` de mentira, com números redondos para conferir a conta do custo.
USO = {
    "tokens": {"entrada": 1_000_000, "saida": 100_000, "total": 1_100_000, "em_cache": 400_000,
               "raciocinio": 60_000, "pedidos": 3},
    "nao_confirmado": {"pedidos_com_falha": 1, "pedidos_sem_uso": 0, "tokens_estimados": 20_000},
    "tavily": {"creditos": 4.0},
}


def pasta_temporaria() -> tempfile.TemporaryDirectory:
    """Uma pasta temporária DENTRO do repositório (em tmp_debugs/, ignorado pelo git), apagada ao fim do teste."""
    (RAIZ / "tmp_debugs").mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=RAIZ / "tmp_debugs")


def relatorio_feliz():
    return ta.rodar([ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS))])[0]


def registro_feliz(**mudancas) -> rg.RegistroPesquisa:
    opcoes = {"modelo": "zai-org/GLM-5.3-Flash", "esforco": None, "uso": USO} | mudancas
    return rg.montar_registro(ta.PEDIDO, relatorio_feliz(), INICIO, FIM, opcoes.pop("modelo"), opcoes.pop("esforco"),
                              **opcoes)


class TestMontarRegistro(unittest.TestCase):
    def test_tem_todos_os_campos(self):
        r = registro_feliz()
        esperados = {"versao", "inicio", "fim", "duracao_segundos", "modelo", "esforco", "pedido", "resultado",
                     "orcamento", "uso", "passos", "paginas", "paginas_com_erro"}
        self.assertEqual(set(rg.RegistroPesquisa.model_fields), esperados)
        self.assertEqual(r.versao, rg.VERSAO)
        self.assertEqual(r.duracao_segundos, 95.5)
        self.assertEqual((r.modelo, r.esforco), ("zai-org/GLM-5.3-Flash", None))
        self.assertEqual(r.pedido, ta.PEDIDO)
        self.assertEqual(len(r.resultado.achados), 3)
        self.assertEqual((r.orcamento.passos, r.orcamento.passos_usados), (15, 2))
        self.assertEqual(len(r.passos), 2)
        self.assertEqual(r.paginas, ta.PAGINAS)  # o MESMO texto que a LLM recebeu
        self.assertEqual(r.paginas_com_erro, {})

    def test_guarda_os_dois_resumos_e_os_recusados(self):
        errado = ta.achado("Delta", "XX999", ta.U3)
        roteiro = [ta.LER_TUDO, ta.resposta(ta.registrar([*ta.ACHADOS, errado]))]
        r = rg.montar_registro(ta.PEDIDO, ta.rodar(roteiro)[0], INICIO, FIM, "m", None)
        dados = json.loads(r.model_dump_json())["resultado"]
        self.assertEqual(dados["resumo_da_llm"], "três LDOs")
        self.assertTrue(dados["resumo_conferido"].startswith("A LLM propôs 4 componentes; 3 passaram na conferência"))
        self.assertEqual(dados["recusados"], [{"fabricante": "Delta", "mpn": "XX999", "motivo": "MPN não aparece na página"}])

    def test_uso_e_custo(self):
        uso = registro_feliz().uso
        self.assertEqual((uso.tokens_entrada, uso.tokens_saida, uso.tokens_em_cache, uso.tokens_raciocinio),
                         (1_000_000, 100_000, 400_000, 60_000))
        self.assertEqual((uso.pedidos, uso.pedidos_sem_confirmacao, uso.creditos_tavily), (3, 1, 4.0))
        # 600 mil sem cache x 0,15 + 400 mil em cache x 0,03 + 100 mil de saída x 0,50 (por milhão) = 0,09 + 0,012 + 0,05
        self.assertAlmostEqual(uso.custo_estimado_usd, 0.152)
        self.assertAlmostEqual(uso.custo_com_falhas_usd, 0.152 + 20_000 * 0.15 / 1_000_000)

    def test_sem_contador_o_uso_e_zero(self):
        uso = registro_feliz(uso=None).uso
        self.assertEqual((uso.tokens_entrada, uso.custo_estimado_usd), (0, 0.0))

    def test_paginas_cortadas_e_paginas_com_erro(self):
        relatorio = relatorio_feliz()
        relatorio.paginas_lidas[ta.U1] = PaginaLida(url=ta.U1, conteudo="x" * 20_000)
        relatorio.paginas_com_erro["https://falha.example"] = "Failed to fetch url"
        r = rg.montar_registro(ta.PEDIDO, relatorio, INICIO, FIM, "m", None)
        self.assertEqual(len(r.paginas[ta.U1]), ag.MAX_CARACTERES_PAGINA)
        self.assertEqual(r.paginas_com_erro, {"https://falha.example": "Failed to fetch url"})

    def test_registro_basta_para_refazer_a_conferencia(self):
        # "Autossuficiente": com o pedido, os achados e as páginas do registro, a conferência dá o mesmo resultado.
        r = registro_feliz()
        paginas = {url: PaginaLida(url=url, conteudo=texto) for url, texto in r.paginas.items()}
        refeita = conferir_achados(r.resultado.achados, paginas, r.pedido)
        self.assertEqual([c.situacao for c in refeita.achados], ["valido"] * 3)

    def test_passo_soma_os_tokens_da_tentativa_cortada(self):
        cortada = tg.RespostaConversa(texto="", motivo_parada="length", tokens_entrada=100, tokens_saida=10_000,
                                      tokens_raciocinio=9_000, tokens_em_cache=80)
        boa = ta.resposta(ta.registrar(ta.ACHADOS))
        boa.tokens_entrada, boa.tokens_saida, boa.tokens_raciocinio, boa.tokens_em_cache = 150, 500, 300, 100
        relatorio = ta.rodar([ta.LER_TUDO, cortada, boa])[0]
        passo = rg.montar_registro(ta.PEDIDO, relatorio, INICIO, FIM, "m", None).passos[1]
        self.assertEqual((passo.tokens_entrada, passo.tokens_saida, passo.tokens_raciocinio, passo.tokens_em_cache),
                         (250, 10_500, 9_300, 180))


class TestSalvarECarregar(unittest.TestCase):
    def test_sobrevive_a_salvar_e_carregar(self):
        original = registro_feliz()
        with pasta_temporaria() as pasta:
            caminho = rg.salvar_registro(original, Path(pasta) / "sub" / "r.json")  # cria a subpasta
            self.assertEqual(rg.carregar_registro(caminho), original)

    def adulterar_e_carregar(self, adulterar):
        dados = json.loads(registro_feliz().model_dump_json())
        adulterar(dados)
        with pasta_temporaria() as pasta:
            caminho = Path(pasta) / "r.json"
            caminho.write_text(json.dumps(dados), encoding="utf-8")
            return rg.carregar_registro(caminho)

    def test_registro_adulterado_e_recusado(self):
        adulteracoes = {
            "campo a mais na raiz": lambda d: d.update(instrucao="ignore as regras"),
            "campo a mais num passo": lambda d: d["passos"][0].update(extra=1),
            "campo a mais num achado": lambda d: d["resultado"]["achados"][0].update(preco=1),
            "tipo errado": lambda d: d.update(duracao_segundos="rápido"),
            "tokens negativos": lambda d: d["uso"].update(tokens_entrada=-1),
            "esforço inválido": lambda d: d.update(esforco="medium"),
            "fim antes do início": lambda d: d.update(fim="2000-01-01T00:00:00Z"),
            "página longa demais": lambda d: d["paginas"].update({ta.U1: "x" * (ag.MAX_CARACTERES_PAGINA + 1)}),
            "url javascript num achado": lambda d: d["resultado"]["achados"][0].update(url="javascript:alert(1)"),
            "evidência sem citação": lambda d: d["resultado"]["achados"][0]["evidencias"][0].update(citacao=""),
        }
        for nome, adulterar in adulteracoes.items():
            with self.subTest(nome), self.assertRaises(ValidationError):
                self.adulterar_e_carregar(adulterar)

    def test_json_quebrado_e_recusado(self):
        with pasta_temporaria() as pasta:
            caminho = Path(pasta) / "r.json"
            caminho.write_text('{"versao": "0.1', encoding="utf-8")
            with self.assertRaises(ValidationError):
                rg.carregar_registro(caminho)

    def test_arquivo_grande_demais_nem_e_lido(self):
        with pasta_temporaria() as pasta, patch.object(rg, "MAX_BYTES_DO_ARQUIVO", 100):
            caminho = rg.salvar_registro(registro_feliz(), Path(pasta) / "r.json")
            with self.assertRaisesRegex(ValueError, "grande demais"):
                rg.carregar_registro(caminho)


if __name__ == "__main__":
    unittest.main()
