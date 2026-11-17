"""Testes da interface Streamlit com `AppTest` (sem rede, sem chaves reais, sem `.env`, sem o secrets.toml real).

    python -m unittest tests.test_frontend -v

O `AppTest` roda o `frontend/app.py` de verdade, num navegador simulado: clicamos em botões, marcamos caixas e lemos o
que apareceu. A pesquisa é trocada por uma função falsa que devolve um resultado PRONTO, na hora.
"""

import inspect
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import streamlit
from streamlit.testing.v1 import AppTest

from backend.compartilhado.modelos import ROTULO_RESUMO_DA_LLM, AchadoRecusado
from backend.servico import pesquisa as servico
from frontend import estado, execucao, protecao
from tests import test_servico

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "app.py")
EXECUCAO = None  # uma execução feliz (3 achados válidos), montada uma vez com a LLM e o Tavily simulados


def setUpModule():
    global EXECUCAO
    EXECUCAO = test_servico.execucao_feliz()


def com_resultado(**mudancas) -> servico.ExecucaoPesquisa:
    """A execução feliz com o RESULTADO alterado (`model_copy` não revalida: dá para montar casos extremos)."""
    resultado = EXECUCAO.registro.resultado.model_copy(update=mudancas)
    return servico.ExecucaoPesquisa(EXECUCAO.relatorio, EXECUCAO.registro.model_copy(update={"resultado": resultado}))


class BaseApp(unittest.TestCase):
    """Cada teste começa sem senha configurada e com a pesquisa falsa no lugar da real."""

    execucao_falsa = None  # cada classe pode trocar
    usar_segredos_do_teste = False  # True: lê a senha pelo caminho REAL (st.secrets), preenchido com `at.secrets`

    def setUp(self):
        self.chamadas = []
        patchers = [patch.object(servico, "executar_pesquisa", side_effect=self.pesquisa_falsa)]
        if not self.usar_segredos_do_teste:  # sem isto, o app leria o .streamlit/secrets.toml REAL de quem roda
            patchers.append(patch.object(protecao, "_senha_dos_segredos", return_value=None))
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def pesquisa_falsa(self, pedido, cancelar=None, orcamento=None, **opcoes):
        self.chamadas.append(pedido)
        orcamento.passos_usados, orcamento.buscas_usadas, orcamento.leituras_usadas = 2, 1, 3
        return self.execucao_falsa or EXECUCAO

    def abrir(self, **estado_inicial) -> AppTest:
        at = AppTest.from_file(APP, default_timeout=15)
        for chave, valor in estado_inicial.items():
            at.session_state[chave] = valor
        at.run()
        self.assertFalse(at.exception, at.exception)
        return at

    def clicar(self, at: AppTest, chave: str) -> AppTest:
        at.button(key=chave).click().run()
        self.assertFalse(at.exception, at.exception)
        return at

    def esperar_pesquisa(self, at: AppTest) -> AppTest:
        """A pesquisa roda numa thread: esperamos ela acabar e rodamos a tela de novo (como o fragmento faria)."""
        at.session_state[estado.TRABALHO].thread.join(timeout=10)
        at.run()
        self.assertFalse(at.exception, at.exception)
        return at

    def na_revisao(self, execucao_pronta) -> AppTest:
        """Abre o app direto na revisão, com o registro dado e os primeiros (até 5) escolhidos."""
        registro = execucao_pronta.registro
        n = min(len(registro.resultado.achados), 5)
        return self.abrir(**{estado.ETAPA: "revisao", estado.REGISTRO: registro, estado.ESCOLHIDOS: set(range(n))})

    @staticmethod
    def textos(at: AppTest) -> list[str]:
        return [t.value for t in at.text]


# =====================================================================================================================
class TestPedido(BaseApp):
    def test_pedido_invalido_desabilita_pesquisar_e_mostra_o_erro(self):
        at = self.abrir()
        self.assertTrue(at.button(key="botao_pesquisar").disabled)
        erros = [e.value for e in at.error]
        self.assertIn("Categoria: não pode ficar vazio", erros)
        self.assertIn("Requisitos: informe pelo menos 1 requisito", erros)

    def test_carregar_exemplo_preenche_o_pedido(self):
        at = self.clicar(self.abrir(), "botao_exemplo")
        self.assertEqual(at.text_input(key=estado.CAMPO_CATEGORIA).value, "regulador linear (LDO)")
        self.assertEqual(at.text_input(key=estado.CAMPO_ENCAPSULAMENTO).value, "SOT-223")
        self.assertEqual([e.value for e in at.error], [])
        self.assertFalse(at.button(key="botao_pesquisar").disabled)

    def test_valor_maximo_vazio_aparece_em_branco_e_nao_como_none(self):
        import pyarrow as pa

        at = self.clicar(self.abrir(), "botao_exemplo")
        (editor,) = at.get("dataframe")  # o `st.data_editor` aparece no AppTest como um elemento "dataframe"
        tabela = pa.ipc.open_stream(editor.proto.arrow_data.data).read_all()  # os dados que vão ao navegador (Arrow)
        self.assertTrue(pa.types.is_floating(tabela.schema.field("valor máximo").type))  # número, não texto
        self.assertEqual(tabela.column("valor máximo").to_pylist(), [None, None])  # nulo = célula em branco
        celulas = [v for linha in tabela.to_pylist() for v in linha.values()]
        self.assertNotIn("None", celulas)  # nenhuma célula com o TEXTO "None" (o defeito antigo)
        self.assertEqual(tabela.column("operador").to_pylist(), ["=", "≥"])

    def test_apagar_a_categoria_volta_a_desabilitar(self):
        at = self.clicar(self.abrir(), "botao_exemplo")
        at.text_input(key=estado.CAMPO_CATEGORIA).set_value("   ").run()
        self.assertTrue(at.button(key="botao_pesquisar").disabled)
        self.assertIn("Categoria: não pode ficar vazio", [e.value for e in at.error])


class TestFluxoCompleto(BaseApp):
    def test_pedido_pesquisa_revisao_resultado(self):
        at = self.clicar(self.abrir(), "botao_exemplo")
        at = self.clicar(at, "botao_pesquisar")
        # A pesquisa falsa é instantânea: no mesmo rerun do clique ela pode já ter acabado (e o app ir à revisão).
        self.assertIn(at.session_state[estado.ETAPA], ("pesquisa", "revisao"))
        self.assertEqual(len(self.chamadas), 1)
        self.assertEqual(self.chamadas[0].categoria, "regulador linear (LDO)")

        at = self.esperar_pesquisa(at)
        self.assertEqual([h.value for h in at.header], ["3. Revisão"])
        self.assertEqual([c.value for c in at.checkbox], [True, True, True])  # padrão: todos os válidos
        self.assertIn(EXECUCAO.registro.resultado.resumo_conferido, self.textos(at))

        at = self.clicar(at, "botao_gerar")
        self.assertEqual([h.value for h in at.header], ["4. Resultado"])
        self.assertTrue(at.session_state[estado.PACOTE].startswith(b"PK"))  # um .zip de verdade
        self.assertIn("Pesquisas restantes nesta sessão: 9", [c.value for c in at.sidebar.caption])

        at = self.clicar(at, "botao_nova")
        self.assertEqual([h.value for h in at.header], ["1. Pedido"])
        self.assertNotIn(estado.REGISTRO, at.session_state)
        self.assertEqual(at.text_input(key=estado.CAMPO_CATEGORIA).value, "regulador linear (LDO)")  # o pedido fica

    def test_menu_destaca_a_etapa_atual(self):
        at = self.na_revisao(EXECUCAO)
        menu = [m.value for m in at.sidebar.markdown]
        self.assertIn("**▶ 3. Revisão**", menu)
        self.assertIn("1. Pedido", menu)


class TestPesquisaEmAndamento(BaseApp):
    def pesquisa_falsa(self, pedido, cancelar=None, orcamento=None, **opcoes):
        """Uma pesquisa que só termina quando o usuário cancela (até 10 s)."""
        orcamento.passos_usados = 4
        for _ in range(1000):
            if cancelar():
                break
            threading.Event().wait(0.01)
        return com_resultado(achados=[], alertas=["pesquisa cancelada pelo usuário"])

    def test_progresso_cancelar_e_voltar(self):
        at = self.clicar(self.clicar(self.abrir(), "botao_exemplo"), "botao_pesquisar")
        trabalho = at.session_state[estado.TRABALHO]
        for _ in range(200):  # a thread é independente: esperamos ela atualizar o orçamento (no máx. 2 s)
            if trabalho.orcamento.passos_usados == 4:
                break
            threading.Event().wait(0.01)
        at.run()  # o que o fragmento faria sozinho a cada 2 s
        self.assertEqual([p.text for p in at.get("progress")], ["Passos: 4 de 15", "Buscas: 0 de 10", "Leituras: 0 de 15"])
        at = self.clicar(at, "botao_cancelar")
        at = self.esperar_pesquisa(at)
        self.assertEqual([h.value for h in at.header], ["2. Pesquisa"])  # parcial: fica aqui, com a explicação
        self.assertTrue(any("interrompida" in w.value for w in at.warning))
        at = self.clicar(at, "botao_voltar")
        self.assertEqual([h.value for h in at.header], ["1. Pedido"])


class TestRevisao(BaseApp):
    def seis_achados(self):
        base = EXECUCAO.registro.resultado.achados[0]
        return com_resultado(achados=[base.model_copy(update={"mpn": f"PECA{i}"}) for i in range(6)])

    def test_escolher_0_ou_6_desabilita_gerar(self):
        at = self.na_revisao(self.seis_achados())
        self.assertEqual([c.value for c in at.checkbox], [True] * 5 + [False])
        self.assertFalse(at.button(key="botao_gerar").disabled)  # 5: pode

        at.checkbox[5].check().run()  # 6
        self.assertTrue(at.button(key="botao_gerar").disabled)
        self.assertIn("Escolha de 1 a 5 componentes para gerar o resultado (escolhidos: 6).",
                      [c.value for c in at.caption])

        for caixa in at.checkbox:
            caixa.uncheck()
        at.run()  # 0
        self.assertTrue(at.button(key="botao_gerar").disabled)
        self.assertIn("Escolha de 1 a 5 componentes para gerar o resultado (escolhidos: 0).",
                      [c.value for c in at.caption])

    def test_recusados_e_comentario_da_llm_aparecem_rotulados(self):
        recusado = AchadoRecusado(fabricante="Diodes", mpn="AP2114H-3.3TRG1", motivo="MPN não aparece na página")
        at = self.na_revisao(com_resultado(recusados=[recusado], resumo_da_llm="Encontrei 4 LDOs ótimos."))
        rotulos = [e.label for e in at.expander]
        self.assertEqual(rotulos, ["Recusados na conferência", ROTULO_RESUMO_DA_LLM])
        recusados, comentario = at.expander
        self.assertIn("AP2114H-3.3TRG1 (Diodes): MPN não aparece na página", [t.value for t in recusados.text])
        self.assertIn("Encontrei 4 LDOs ótimos.", [t.value for t in comentario.text])

    def test_texto_da_web_aparece_literal(self):
        base = EXECUCAO.registro.resultado.achados[0]
        evidencia = base.evidencias[0].model_copy(update={"citacao": "**negrito** e [link](http://mal.example)"})
        perigoso = base.model_copy(update={"fabricante": "<script>alert(1)</script>", "evidencias": [evidencia]})
        at = self.na_revisao(com_resultado(achados=[perigoso], resumo_da_llm="<b>oi</b>"))
        textos = self.textos(at)
        self.assertIn("<script>alert(1)</script>", textos)  # aparece como TEXTO...
        self.assertIn("**negrito** e [link](http://mal.example)", textos)
        markdown = " ".join(m.value for m in at.markdown)
        for perigo in ("<script>", "negrito", "mal.example", "<b>oi"):  # ...e nunca vai para o markdown
            self.assertNotIn(perigo, markdown)
        self.assertIn(base.url, [c.value for c in at.code])  # a URL vai num st.code (copiável, sem link)


class TestComSenha(BaseApp):
    usar_segredos_do_teste = True

    def abrir_com_senha(self) -> AppTest:
        at = AppTest.from_file(APP, default_timeout=15)
        at.secrets["senha_de_acesso"] = "senha-certa"
        at.run()
        return at

    def test_com_senha_pede_e_recusa_a_errada(self):
        at = self.abrir_com_senha()
        self.assertEqual([h.value for h in at.header], [])  # nada do app aparece
        self.assertIsNotNone(at.text_input(key=estado.CAMPO_SENHA))
        at.text_input(key=estado.CAMPO_SENHA).input("errada")
        at.button(key="botao_entrar").click().run()
        self.assertEqual([e.value for e in at.error], ["Senha incorreta."])
        self.assertEqual([h.value for h in at.header], [])

        at.text_input(key=estado.CAMPO_SENHA).input("senha-certa")
        at.button(key="botao_entrar").click().run()
        self.assertEqual([h.value for h in at.header], ["1. Pedido"])
        self.assertEqual(at.session_state[estado.CAMPO_SENHA], "")  # a senha digitada não fica guardada


class TestSemSenha(BaseApp):
    def test_sem_senha_abre_direto(self):
        at = self.abrir()
        self.assertEqual([h.value for h in at.header], ["1. Pedido"])
        self.assertIn("Acesso aberto: nenhuma senha configurada.", [c.value for c in at.sidebar.caption])


class TestLimitesEChaves(BaseApp):
    def test_limite_de_pesquisas_por_sessao(self):
        at = self.clicar(self.abrir(**{estado.PESQUISAS_FEITAS: protecao.MAX_PESQUISAS_POR_SESSAO}), "botao_exemplo")
        self.assertTrue(at.button(key="botao_pesquisar").disabled)
        self.assertIn("Limite de pesquisas desta sessão atingido.", [w.value for w in at.warning])
        self.assertIn("Pesquisas restantes nesta sessão: 0", [c.value for c in at.sidebar.caption])

    def test_chaves_ausentes_avisam_sem_chamar_a_pesquisa(self):
        sem_chaves = {k: v for k, v in os.environ.items() if k not in execucao.CHAVES_DE_API}
        with patch.dict(os.environ, sem_chaves, clear=True):
            at = self.clicar(self.abrir(), "botao_exemplo")
            self.assertIn("Configure TOGETHER_API_KEY e TAVILY_API_KEY no arquivo .env (veja o README)",
                          [w.value for w in at.warning])
            self.assertTrue(at.button(key="botao_pesquisar").disabled)
        self.assertEqual(self.chamadas, [])


# =====================================================================================================================
class _Explosivo:
    """Um `st.session_state` falso que FALHA a qualquer uso: se a thread tocar nele, o teste acusa."""

    def _falhar(self, *args, **kwargs):
        raise AssertionError("a thread da pesquisa tocou em st.session_state")

    __getattr__ = __getitem__ = __setitem__ = __contains__ = __iter__ = _falhar


class TestThread(unittest.TestCase):
    def rodar_thread(self, pesquisa_falsa) -> execucao.Trabalho:
        with patch.object(streamlit, "session_state", _Explosivo()), \
                patch.object(servico, "executar_pesquisa", side_effect=pesquisa_falsa):
            trabalho = execucao.iniciar(test_servico.ta.PEDIDO)
            trabalho.thread.join(timeout=10)
        self.assertTrue(trabalho.terminou.is_set())
        return trabalho

    def test_a_thread_nao_acessa_session_state(self):
        trabalho = self.rodar_thread(lambda pedido, **kw: EXECUCAO)
        self.assertIsNone(trabalho.erro)
        self.assertIs(trabalho.execucao, EXECUCAO)

    def test_o_teste_acusa_se_a_thread_tocar_no_session_state(self):
        # Controle: uma "pesquisa" que toca no session_state É detectada (vira erro no trabalho).
        def pesquisa_que_toca(pedido, **kw):
            return streamlit.session_state["qualquer"]

        with patch.object(execucao.traceback, "print_exc"):  # o erro é esperado: sem o traceback no terminal
            trabalho = self.rodar_thread(pesquisa_que_toca)
        self.assertEqual(trabalho.erro, "Erro inesperado na pesquisa (AssertionError). Veja o terminal do servidor.")

    def test_modulo_de_execucao_nem_importa_streamlit(self):
        self.assertNotIn("streamlit", inspect.getsource(execucao).split('"""', 2)[2])  # fora da docstring

    def test_erro_conhecido_vira_mensagem_para_o_usuario(self):
        from backend.compartilhado.together_client import ErroTogether

        def falha(pedido, **kw):
            raise ErroTogether("A Together não respondeu a tempo. Tente novamente em instantes.")

        self.assertEqual(self.rodar_thread(falha).erro, "A Together não respondeu a tempo. Tente novamente em instantes.")


if __name__ == "__main__":
    unittest.main()
