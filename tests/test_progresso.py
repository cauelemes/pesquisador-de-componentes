"""Testes dos eventos de progresso: o módulo, o agente, o terminal e a tela (sem rede, sem créditos).

    python -m unittest tests.test_progresso -v
"""

import io
import re
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from backend.compartilhado import together_client as tg
from backend.pesquisa import progresso
from backend.pesquisa import testar_agente
from backend.pesquisa.progresso import ALERTA_CALLBACK, MAX_TEXTO_EVENTO, EventoDeProgresso, emitir, limpar_texto
from backend.servico import pesquisa as servico
from frontend import estado
from tests import test_agente as ta
from tests import test_frontend as tf


EVENTO_NO_TERMINAL = re.compile(r"^\[\d\d:\d\d\] ")  # "[mm:ss] " (e não "[orçamento]", do resumo final)


def setUpModule():
    tf.setUpModule()  # a execução pronta que a tela falsa devolve (montada uma vez, com tudo simulado)


def rodar_com_eventos(roteiro, **opcoes):
    """Roda o agente roteirizado guardando os eventos. Devolve (relatório, eventos)."""
    eventos: list[EventoDeProgresso] = []
    relatorio = ta.rodar(roteiro, ao_evento=eventos.append, **opcoes)[0]
    return relatorio, eventos


class TestModulo(unittest.TestCase):
    def test_texto_longo_e_com_quebras_e_cortado_e_limpo(self):
        sujo = "linha 1\nlinha 2\r\n\tcom tab \x1b[31mvermelho\x1b[0m e \x00nulo"
        self.assertEqual(limpar_texto(sujo), "linha 1 linha 2 com tab [31mvermelho [0m e nulo")
        longo = limpar_texto("x" * 500)
        self.assertEqual(len(longo), MAX_TEXTO_EVENTO)
        self.assertTrue(longo.endswith("…"))
        self.assertEqual(limpar_texto(None), "")

    def test_sem_callback_nada_acontece(self):
        self.assertIsNone(emitir(None, "busca", "LDO"))

    def test_callback_com_defeito_vira_um_unico_alerta(self):
        alertas = []

        def quebrado(evento):
            raise RuntimeError("defeito no acompanhamento")

        for _ in range(3):
            emitir(quebrado, "busca", "LDO", alertas=alertas)  # não levanta
        self.assertEqual(alertas, [ALERTA_CALLBACK])

    def test_tipo_desconhecido_e_erro_de_programacao(self):
        with self.assertRaises(ValueError):
            emitir(lambda e: None, "outro", "x")

    def test_formatar_tempo(self):
        self.assertEqual([progresso.formatar_tempo(s) for s in (0, 83, 600, 4500.9)], ["00:00", "01:23", "10:00", "75:00"])


class TestAgente(unittest.TestCase):
    def test_ordem_num_ciclo_roteirizado(self):
        roteiro = [ta.resposta(ta.buscar("LDO 3.3 V"), texto="Vou buscar LDOs de 3,3 V."),
                   ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS))]
        _, eventos = rodar_com_eventos(roteiro)
        self.assertEqual([e.tipo for e in eventos], [
            "passo", "fala_da_llm", "busca",
            "passo", "leitura", "leitura", "leitura",
            "passo", "registro",
        ])
        textos = [e.texto for e in eventos]
        self.assertEqual(textos[0], "Passo 1 de 15: aguardando a LLM")
        self.assertEqual(textos[1], "Vou buscar LDOs de 3,3 V.")
        self.assertTrue(textos[2].startswith("LDO 3.3 V ("))
        self.assertEqual(textos[4], ta.U1)
        self.assertEqual(textos[-1], "Registro recebido: 3 válido(s), 0 recusado(s)")
        self.assertEqual([e.segundos for e in eventos], sorted(e.segundos for e in eventos))  # o tempo só anda

    def test_leitura_que_falha_vira_leitura_falhou(self):
        falha = "https://falha.example/datasheet.pdf"
        _, eventos = rodar_com_eventos([ta.resposta(ta.ler(falha)), *ta.FIM_SEM_ACHADOS])
        pares = [(e.tipo, e.texto) for e in eventos]
        self.assertIn(("leitura", falha), pares)
        self.assertIn(("leitura_falhou", f"{falha} (404)"), pares)

    def test_correcoes_e_avisos(self):
        errado = ta.achado("Gama", "XX999", ta.U3)
        roteiro = [ta.resposta(ta.buscar("LDO"), ta.buscar(" ldo ")), ta.TestCortePorTokens.CORTADA, ta.LER_TUDO,
                   ta.resposta(ta.registrar(ta.SEM_CITACAO)), ta.resposta(ta.registrar([*ta.ACHADOS[:2], errado])),
                   ta.resposta(ta.registrar([ta.ACHADOS[2]]))]
        _, eventos = rodar_com_eventos(roteiro)
        pares = [(e.tipo, e.texto) for e in eventos]
        self.assertIn(("aviso", "Consulta repetida recusada: ldo"), pares)
        self.assertIn(("aviso", "Resposta cortada por limite de tokens; pedindo de novo, mais curta"), pares)
        self.assertIn(("correcao", "Registro recusado pela validação (falta o campo `citacao`). A LLM vai corrigir"), pares)
        self.assertIn(("correcao", "A conferência recusou XX999: MPN não aparece na página. A LLM vai corrigir"), pares)

    def test_raciocinio_interno_nao_vira_evento(self):
        com_raciocinio = ta.resposta(ta.ler(ta.U1), texto="Vou ler o datasheet.")
        com_raciocinio.raciocinio = "PENSAMENTO-INTERNO-SECRETO"
        _, eventos = rodar_com_eventos([com_raciocinio, *ta.FIM_SEM_ACHADOS])
        self.assertFalse(any("PENSAMENTO" in e.texto for e in eventos))
        self.assertIn(("fala_da_llm", "Vou ler o datasheet."), [(e.tipo, e.texto) for e in eventos])

    def test_callback_que_levanta_nao_interrompe_a_pesquisa(self):
        def quebrado(evento):
            raise RuntimeError("tela fechada")

        relatorio = ta.rodar([ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS))], ao_evento=quebrado)[0]
        self.assertEqual(len(relatorio.resultado.achados), 3)
        self.assertEqual(relatorio.resultado.alertas.count(ALERTA_CALLBACK), 1)

    def test_texto_da_llm_com_quebras_chega_limpo(self):
        _, eventos = rodar_com_eventos([ta.resposta(ta.ler(ta.U1), texto="linha 1\nlinha 2" + " z" * 300),
                                        *ta.FIM_SEM_ACHADOS])
        fala = next(e.texto for e in eventos if e.tipo == "fala_da_llm")
        self.assertNotIn("\n", fala)
        self.assertLessEqual(len(fala), MAX_TEXTO_EVENTO)

    def test_sem_callback_nada_muda(self):
        com = rodar_com_eventos([ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS))])[0].resultado
        sem = ta.rodar([ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS))])[0].resultado
        self.assertEqual(com, sem)


class TestTerminal(unittest.TestCase):
    def rodar_cli(self, *argumentos) -> str:
        def pesquisar_simulado(pedido, **opcoes):
            return ta.rodar([ta.LER_TUDO, ta.resposta(ta.registrar(ta.ACHADOS))], **opcoes)[0]

        saida = io.StringIO()
        with patch.object(servico, "pesquisar", wraps=pesquisar_simulado), redirect_stdout(saida):
            testar_agente.main([str(ta.EXEMPLO), *argumentos])
        return saida.getvalue()

    def test_uma_linha_por_evento_no_formato_esperado(self):
        linhas = self.rodar_cli().splitlines()
        eventos = [linha for linha in linhas if EVENTO_NO_TERMINAL.match(linha)]
        self.assertGreater(len(eventos), 5)
        for linha in eventos:
            self.assertRegex(linha, r"^\[\d\d:\d\d\] [a-zçãéLM ]+: \S")
        self.assertIn(f"[00:00] leitura: {ta.U1}", eventos)
        self.assertIn("[00:00] passo: Passo 1 de 15: aguardando a LLM", eventos)
        self.assertIn("[00:00] registro: Registro recebido: 3 válido(s), 0 recusado(s)", eventos)
        self.assertIn("Achados válidos: 3", linhas)

    def test_silencioso_nao_imprime_eventos_e_o_resumo_e_igual(self):
        normal, silencioso = self.rodar_cli(), self.rodar_cli("--silencioso")
        self.assertFalse(any(EVENTO_NO_TERMINAL.match(linha) for linha in silencioso.splitlines()))
        resumo = [linha for linha in normal.splitlines() if not EVENTO_NO_TERMINAL.match(linha)]
        self.assertEqual([linha for linha in resumo if not linha.startswith("Uso:")],
                         [linha for linha in silencioso.splitlines() if not linha.startswith("Uso:")])

    def test_batimento_para_ao_fim_da_pesquisa(self):
        self.rodar_cli()
        self.assertNotIn("batimento", [t.name for t in threading.enumerate()])


class RelogioFalso:
    """Um relógio que só anda quando o teste manda (nada de esperar de verdade)."""

    def __init__(self):
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


class TestBatimento(unittest.TestCase):
    def setUp(self):
        self.relogio, self.saida = RelogioFalso(), io.StringIO()
        self.acompanhamento = testar_agente.AcompanhamentoNoTerminal(saida=self.saida, relogio=self.relogio)

    def em(self, segundos: float) -> list[str]:
        """Avança o relógio até `segundos` após o início, dá um "tique" e devolve as linhas novas."""
        antes = len(self.saida.getvalue().splitlines())
        self.relogio.agora = 1000.0 + segundos
        self.acompanhamento.verificar_silencio()
        return self.saida.getvalue().splitlines()[antes:]

    def test_avisa_a_cada_30_s_de_silencio(self):
        self.assertEqual(self.em(29), [])
        self.assertEqual(self.em(31), ["[00:31] ainda aguardando a LLM (31 s sem novidades; chamadas com raciocínio "
                                       "podem levar alguns minutos)"])
        self.assertEqual(self.em(45), [])  # o próximo aviso só aos 60 s de silêncio
        self.assertEqual(self.em(61)[0][:46], "[01:01] ainda aguardando a LLM (61 s sem novid")

    def test_um_evento_zera_o_silencio(self):
        self.em(31)
        self.relogio.agora = 1000.0 + 40
        self.acompanhamento(EventoDeProgresso(40.0, "busca", "LDO"))
        self.assertEqual(self.em(65), [])  # só 25 s desde o evento
        self.assertEqual(len(self.em(71)), 1)  # 31 s desde o evento

    def test_a_thread_do_batimento_para(self):
        self.acompanhamento.iniciar_batimento(a_cada=0.01)
        self.assertTrue(self.acompanhamento.thread.is_alive())
        self.acompanhamento.parar_batimento()
        self.assertFalse(self.acompanhamento.thread.is_alive())


# =====================================================================================================================
class TestTela(tf.BaseApp):
    eventos_a_emitir: list[tuple[str, str]] = []

    def pesquisa_falsa(self, pedido, cancelar=None, orcamento=None, ao_evento=None, **opcoes):
        """Emite os eventos do roteiro e fica "pesquisando" até o usuário cancelar (no máximo 10 s)."""
        for i, (tipo, texto) in enumerate(self.eventos_a_emitir):
            emitir(ao_evento, tipo, texto, segundos=i)
        for _ in range(1000):
            if cancelar():
                break
            threading.Event().wait(0.01)
        return tf.com_resultado(achados=[], alertas=["pesquisa cancelada pelo usuário"])

    def iniciar_e_esperar_eventos(self):
        at = self.clicar(self.clicar(self.abrir(), "botao_exemplo"), "botao_pesquisar")
        trabalho = at.session_state[estado.TRABALHO]
        for _ in range(200):  # esperamos a thread emitir tudo (no máximo 2 s)
            if len(trabalho.eventos) == len(self.eventos_a_emitir):
                break
            threading.Event().wait(0.01)
        at.run()  # o que o fragmento faria a cada 2 s
        self.addCleanup(lambda: (trabalho.cancelamento.set(), trabalho.thread.join(5)))
        return at

    def test_sem_eventos_mostra_iniciando(self):
        self.eventos_a_emitir = []
        at = self.iniciar_e_esperar_eventos()
        self.assertIn("Iniciando...", self.textos(at))
        self.assertTrue(any(c.value.startswith("Tempo decorrido: ") for c in at.caption))

    def test_painel_com_os_ultimos_8_mais_novo_primeiro(self):
        self.eventos_a_emitir = [("busca", f"consulta {i}") for i in range(10)]
        at = self.iniciar_e_esperar_eventos()
        textos = [t for t in self.textos(at) if t.startswith("consulta ")]
        self.assertEqual(textos, [f"consulta {i}" for i in range(9, 1, -1)])  # 9, 8, ..., 2
        self.assertIn("Atividade recente", " ".join(m.value for m in at.markdown))
        self.assertIn("Busca", [c.value for c in at.caption])  # o rótulo fixo do tipo

    def test_texto_do_evento_aparece_literal(self):
        self.eventos_a_emitir = [("fala_da_llm", "**negrito** e <script>alert(1)</script>")]
        at = self.iniciar_e_esperar_eventos()
        self.assertIn("**negrito** e <script>alert(1)</script>", self.textos(at))
        markdown = " ".join(m.value for m in at.markdown)
        self.assertNotIn("negrito", markdown)
        self.assertNotIn("<script>", markdown)


if __name__ == "__main__":
    unittest.main()
