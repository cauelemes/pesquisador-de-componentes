"""Testes do modo demonstração: o replay, o sanitizador e o arquivo de exemplo (sem rede, sem chaves, sem `.env`).

    python -m unittest tests.test_demo -v

As "gravações" daqui são montadas pelo próprio pipeline simulado (pequenas, feitas para o teste): a gravação real
de `tmp_debugs/` nunca é usada nos testes.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.compartilhado import together_client as tg
from backend.compartilhado import uso as contador_de_uso
from backend.compartilhado.modelos import PaginaLida
from backend.pesquisa import demo
from backend.pesquisa import sanitizar_registro as sz
from backend.pesquisa import tavily_client
from backend.pesquisa.registro import carregar_registro, registro_em_json, salvar_registro
from backend.servico import pesquisa as servico
from tests import test_agente as ta
from tests.test_registro import pasta_temporaria

EXEMPLO_PUBLICO = Path(__file__).resolve().parent.parent / "exemplos" / "execucao_gravada.json"
FALHA = "https://falha.example/datasheet.pdf"  # o `falso_extrair` devolve erro "404" para ela
ERRADO = ta.achado("Gama", "XX999", ta.U3)  # a página da Gama não cita "XX999": recusado
# Um roteiro com tudo que o replay precisa reproduzir: corte por tokens, fala, busca, leitura que falha, recusado, revisão.
ROTEIRO = [
    ta.TestCortePorTokens.CORTADA,
    ta.resposta(ta.buscar("LDO 3.3 V SOT-223"), texto="Vou buscar LDOs."),
    ta.resposta(ta.ler(ta.U1), ta.ler(ta.U2), ta.ler(ta.U3), ta.ler(FALHA)),
    ta.resposta(ta.registrar([*ta.ACHADOS[:2], ERRADO])),
    ta.resposta(ta.registrar([ta.ACHADOS[2]])),
]
SENSIVEL = " Contato: vendas@exemplo.com · chave tvly-ABCdef123 e tgp_SEGREDO42 · arquivo /home/fulano/notas.txt ."


def gravar(roteiro=ROTEIRO, extrair=ta.falso_extrair):
    """Uma "execução real" com a LLM e o Tavily simulados, pelo serviço de verdade. Devolve o registro gravado."""
    fila = iter(roteiro)

    def falsa_llm(mensagens, **opcoes):
        item = next(fila)
        if isinstance(item, Exception):
            raise item
        return item

    with (
        patch.object(tg, "conversar", side_effect=falsa_llm),
        patch.object(tavily_client, "buscar", side_effect=ta.falso_buscar),
        patch.object(tavily_client, "extrair", side_effect=extrair),
    ):
        return servico.executar_pesquisa(ta.PEDIDO).registro


def sem_apis():
    """Os clientes REAIS trocados por funções que FALHAM: se a demonstração chamar algum, o teste acusa."""
    def proibido(*args, **kwargs):
        raise AssertionError("a demonstração chamou uma API real")

    return (patch.object(tg, "conversar", proibido), patch.object(tavily_client, "buscar", proibido),
            patch.object(tavily_client, "extrair", proibido))


def repetir_sem_apis(gravacao, **opcoes):
    a, b, c = sem_apis()
    with a, b, c:
        return demo.repetir(gravacao, **opcoes)


class TestReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gravacao = gravar()

    def test_a_gravacao_de_teste_tem_o_que_interessa(self):
        r = self.gravacao.resultado
        self.assertEqual([a.mpn for a in r.achados], ["AC1117-3.3", "BT33", "GM3301"])
        self.assertEqual([x.mpn for x in r.recusados], ["XX999"])
        self.assertIn(FALHA, self.gravacao.paginas_com_erro)
        self.assertEqual(self.gravacao.passos[0].cortes_por_tokens, 1)

    def test_reproduz_exatamente_o_resultado_gravado(self):
        execucao = repetir_sem_apis(self.gravacao)  # os clientes reais FALHARIAM se fossem chamados
        refeito = execucao.registro
        self.assertEqual(refeito.resultado.achados, self.gravacao.resultado.achados)  # com as evidências
        self.assertEqual(refeito.resultado.recusados, self.gravacao.resultado.recusados)
        self.assertEqual(refeito.resultado.alertas, self.gravacao.resultado.alertas)
        self.assertEqual(refeito.resultado.resumo_conferido, self.gravacao.resultado.resumo_conferido)
        self.assertEqual(refeito.paginas, self.gravacao.paginas)
        self.assertEqual(len(refeito.passos), len(self.gravacao.passos))
        self.assertTrue(refeito.demonstracao)
        self.assertEqual(refeito.uso.tokens_entrada, 0)  # nenhuma API: nenhum token gasto

    def test_eventos_e_atraso_por_passo(self):
        eventos, esperas = [], []
        repetir_sem_apis(self.gravacao, ao_evento=eventos.append, atraso=0.4, dormir=esperas.append)
        self.assertIn(("fala_da_llm", "Vou buscar LDOs."), [(e.tipo, e.texto) for e in eventos])
        self.assertEqual(set(esperas), {0.4})  # o atraso é pedido a cada "resposta da LLM" (sem esperar de verdade)

    def test_pedido_fora_da_gravacao_falha_com_mensagem_clara(self):
        replay = demo.Replay(self.gravacao)
        with self.assertRaisesRegex(demo.ErroDeReplay, "não tem a busca 'outra consulta'"):
            replay.buscar("outra consulta")
        with self.assertRaisesRegex(demo.ErroDeReplay, "não tem a página 'https://outra.example/x'"):
            replay.ler(["https://outra.example/x"], foco="")

    def test_gravacao_que_acaba_antes_falha(self):
        curta = self.gravacao.model_copy(update={"passos": self.gravacao.passos[:3]})
        with self.assertRaisesRegex(demo.ErroDeReplay, "A gravação acabou"):
            repetir_sem_apis(curta)

    def test_passo_forcado_fora_do_lugar_e_divergencia(self):
        orcamento = self.gravacao.orcamento.model_copy(update={"passos": 2})  # forçaria o registro cedo demais
        with self.assertRaisesRegex(demo.ErroDeReplay, "Divergência"):
            repetir_sem_apis(self.gravacao.model_copy(update={"orcamento": orcamento}))

    def test_gravacao_com_falha_da_llm_repete_a_mesma_falha(self):
        tempo = tg._traduzir_erro(type("APITimeoutError", (Exception,), {})())
        gravacao = gravar([ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS[:1], definitivo=False)), tempo])
        refeito = repetir_sem_apis(gravacao).registro.resultado
        self.assertEqual(refeito.alertas, gravacao.resultado.alertas)
        self.assertTrue(any("não respondeu a tempo" in a for a in refeito.alertas))

    def test_gravacao_cancelada_nao_e_repetida(self):
        resultado = self.gravacao.resultado.model_copy(update={"alertas": ["pesquisa cancelada pelo usuário"]})
        with self.assertRaisesRegex(demo.ErroDeReplay, "CANCELADA"):
            demo.Replay(self.gravacao.model_copy(update={"resultado": resultado}))

    def test_nao_ativa_contador_de_uso_fora_da_pesquisa(self):
        repetir_sem_apis(self.gravacao)
        self.assertIsNone(contador_de_uso.coletor_ativo())


class TestCliDemo(unittest.TestCase):
    def test_primeira_linha_e_o_aviso_e_o_resumo_e_o_do_cli_real(self):
        with pasta_temporaria() as pasta:
            caminho = salvar_registro(gravar(), Path(pasta) / "g.json")
            saida = io.StringIO()
            a, b, c = sem_apis()
            with a, b, c, redirect_stdout(saida):
                codigo = demo.main([str(caminho), "--silencioso"])
        linhas = saida.getvalue().splitlines()
        self.assertEqual(codigo, 0)
        self.assertEqual(linhas[0], "DEMONSTRAÇÃO: repetição de uma execução gravada; nenhuma API foi chamada")
        self.assertIn("Achados válidos: 3", linhas)
        self.assertTrue(any(linha.startswith("Resumo conferido: A LLM propôs 4 componentes") for linha in linhas))

    def test_arquivo_ausente_explica_como_gerar(self):
        erro = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(erro):
            codigo = demo.main(["tmp_debugs/nao-existe.json"])
        self.assertEqual(codigo, 1)
        self.assertIn("python -m backend.pesquisa.sanitizar_registro", erro.getvalue())


class TestSanitizador(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Páginas LONGAS (como um datasheet), com dados sensíveis no meio de muito texto que não prova nada.
        def extrair_longo(urls, *, foco):
            return [PaginaLida(url=u, conteudo=("Introdução genérica. " * 120) + ta.PAGINAS[u] + SENSIVEL
                               + (" Tabela de pinos e notas de aplicação." * 120)) if u in ta.PAGINAS
                    else PaginaLida(url=u, erro="404") for u in urls]

        cls.original = gravar(extrair=extrair_longo)
        cls.recortado = sz.sanitizar(cls.original)

    def test_recorta_as_paginas_e_os_passos(self):
        for url, texto in self.original.paginas.items():
            with self.subTest(url=url):
                self.assertLess(len(self.recortado.paginas[url]), len(texto) / 2)
                self.assertIn("Output voltage: 3.3 V", self.recortado.paginas[url])  # o que prova a citação fica
        leituras = [f.resultado for p in self.recortado.passos for f in p.ferramentas if f.nome == "ler_pagina"]
        # O enchimento longo sumiu (30 repetições = ~630 caracteres, mais que a MARGEM de 300 em volta da citação),
        # tanto na página quanto na cópia dela que o passo guarda.
        longo = "Introdução genérica. " * 30
        self.assertTrue(all(longo not in texto for texto in self.recortado.paginas.values()))
        self.assertTrue(leituras and all(longo not in r for r in leituras))

    def test_corta_os_resultados_de_busca(self):
        for p in self.recortado.passos:
            for f in p.ferramentas:
                if f.nome == "buscar_na_web" and f.resultado.startswith("{"):
                    for item in json.loads(f.resultado)["resultados"]:
                        self.assertLessEqual(len(item["titulo"]) + len(item["url"]) + len(item["trecho"]), 600)

    def test_remove_chaves_emails_e_caminhos(self):
        texto = registro_em_json(self.recortado).decode("utf-8")
        for proibido in ("vendas@exemplo.com", "tvly-", "tgp_", "/home/fulano"):
            self.assertNotIn(proibido, texto)
        self.assertIn(sz.REMOVIDO, texto)

    def test_conferencia_identica_e_marcado_como_demonstracao(self):
        self.assertTrue(self.recortado.demonstracao)
        self.assertEqual(sz._impressao_digital(repetir_sem_apis(self.recortado).registro.resultado),
                         sz._impressao_digital(self.original.resultado))

    def test_recorte_que_mudaria_a_conferencia_e_recusado(self):
        with patch.object(sz, "recortar_pagina", lambda texto, alvos: "página sem nada"):
            with self.assertRaisesRegex(sz.ErroDeSanitizacao, "mudaria a conferência"):
                sz.sanitizar(self.original)

    def test_trechos_que_se_tocam_viram_um_so(self):
        texto = "a" * 1000 + "ALVO1" + "b" * 100 + "ALVO2" + "c" * 1000 + "ALVO3" + "d" * 1000
        recorte = sz.recortar_pagina(texto, {"ALVO1", "ALVO2", "ALVO3"})
        self.assertEqual(recorte.count(sz.SEPARADOR), 1)  # ALVO1 e ALVO2 juntos; ALVO3 separado
        self.assertIn("ALVO1" + "b" * 100 + "ALVO2", recorte)

    def test_hifenizacao_de_pdf_continua_achada(self):
        texto = "x" * 800 + "Low Dropout Voltage: 1.15V at 1A Output Cur-\nrent." + "y" * 800
        recorte = sz.recortar_pagina(texto, {"Low Dropout Voltage: 1.15V at 1A Output Current"})
        self.assertIn("Output Cur-\nrent", recorte)  # achado pela forma de reserva e mantido no original


class TestExemploPublico(unittest.TestCase):
    def test_arquivo_de_exemplo_carrega_e_passa_na_conferencia(self):
        if not EXEMPLO_PUBLICO.exists():
            self.fail("Falta exemplos/execucao_gravada.json. Gere com: python -m backend.pesquisa.sanitizar_registro "
                      "tmp_debugs/gravada.json exemplos/execucao_gravada.json")
        gravacao = carregar_registro(EXEMPLO_PUBLICO)
        self.assertTrue(gravacao.demonstracao)
        self.assertLess(EXEMPLO_PUBLICO.stat().st_size, 150 * 1024)
        refeito = repetir_sem_apis(gravacao).registro.resultado
        self.assertEqual(sz._impressao_digital(refeito), sz._impressao_digital(gravacao.resultado))
        self.assertTrue(gravacao.resultado.achados)
        texto = EXEMPLO_PUBLICO.read_text(encoding="utf-8")
        for padrao in sz.PADROES_SENSIVEIS:
            self.assertIsNone(padrao.search(texto), padrao.pattern)


if __name__ == "__main__":
    unittest.main()
