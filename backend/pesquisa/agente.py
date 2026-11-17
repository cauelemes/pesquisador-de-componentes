"""Ciclo do agente de pesquisa por "function calling": a LLM busca, lê e registra; o código confere.

Como funciona um ciclo de function calling:
1. Mandamos à LLM o histórico da conversa e a lista de ferramentas que ela pode pedir.
2. A LLM responde com texto e/ou PEDIDOS de ferramenta ("chame buscar_na_web com consulta=...").
3. NÓS executamos cada ferramenta (a LLM nunca executa nada) e anexamos o resultado ao histórico.
4. Voltamos ao passo 1, até a LLM registrar o resultado ou o orçamento acabar.

A LLM julga (o que buscar, o que ler, o que serve); o código confere (`conferencia.py`): toda citação, MPN e URL
registrada é checada contra as páginas que foram lidas DE VERDADE nesta pesquisa (`self.paginas`).
"""

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from pydantic import ValidationError

# Importamos os MÓDULOS (e não as funções) para que os testes possam trocar `tg.conversar` e
# `tavily_client.buscar` por versões simuladas com `unittest.mock.patch`.
from ..compartilhado import together_client as tg
from ..compartilhado.modelos import (
    MAX_ACHADOS,
    MAX_ALERTA,
    MAX_ALERTAS,
    MAX_MOTIVO,
    MAX_RECUSADOS,
    AchadoComponente,
    AchadoRecusado,
    PaginaLida,
    PedidoComponente,
    ResultadoPesquisa,
)
from . import tavily_client
from .conferencia import alertas_do_comentario, conferir_achados, montar_resumo_conferido, normalizar_texto
from .ferramentas import (  # noqa: F401 (reexportados: quem importava daqui continua funcionando)
    _FORCAR_REGISTRO,
    FERRAMENTAS,
    REDES_SOCIAIS,
    _como_dado,
    _rede_social,
    ArgsBusca,
    ArgsLeitura,
    ArgsRegistro,
    classe_do_erro,
    erros_curtos,
    mensagem_de_registro_invalido,
)
from .progresso import EventoDeProgresso, emitir
from .prompts import _P_ABERTURA, _P_ENTREGA, _P_REGRAS, PROMPT_SISTEMA  # noqa: F401 (os blocos são reexportados)
from .urls import normalizar_url

# Modelo do agente (NÃO é o `MODELO_PADRAO` do cliente). O GLM-5.3-Flash raciocina SEMPRE antes de responder, e esse
# raciocínio sai do mesmo limite de tokens da resposta: por isso o `max_tokens` é bem maior que o padrão do cliente.
MODELO_AGENTE_PESQUISA = "zai-org/GLM-5.3-Flash"
TEMPERATURA_AGENTE = 0.2  # baixa: queremos decisões estáveis, não criatividade
MAX_TOKENS_AGENTE = 10_000
# Espera por CADA chamada à LLM. Com raciocínio, passa fácil de 1 min; o padrão do cliente (60 s) estouraria DEPOIS de
# a chamada já ter gastado créditos. Vale para todas as chamadas do ciclo (normal, nova tentativa e forçada).
TIMEOUT_CHAMADA_SEGUNDOS = 240
# Esforço de raciocínio aceito pelo Flash. "medium" NÃO existe nele. None = não enviar nada (padrão do modelo).
ESFORCOS_ACEITOS = ("low", "high", "max")
_AVISO_CORTE = (
    "Sua resposta anterior foi cortada por limite de tokens e foi descartada. Seja mais breve: raciocine menos e "
    "chame uma ferramenta de cada vez."
)

MAX_CARACTERES_PAGINA = 12_000  # texto de cada página lida que vai para a LLM (e para a conferência)
PREFIXO_FALHA_LLM = "falha ao falar com a LLM"  # começo do alerta de falha: a interface o reconhece por aqui
MIN_ACHADOS_VALIDOS = 3  # abaixo disso, a LLM recebe UMA chance de revisar
MAX_RESULTADOS_BUSCA = 5
MAX_TRECHO_RESULTADO = 500  # quanto do resumo de cada resultado de busca a LLM vê


# O prompt de sistema fica em `prompts.py` (blocos `_P_ABERTURA`, `_P_REGRAS`, `_P_ENTREGA`).


# =====================================================================================================================
# Orçamento e relatório
# =====================================================================================================================
@dataclass
class Orcamento:
    """Limites da pesquisa e quanto já foi usado. É atualizado durante o ciclo (dá para ler depois)."""

    passos: int = 15  # chamadas à LLM
    buscas: int = 10  # no 2º teste real: 7 de 12 buscas usadas, mas 12 de 12 leituras -> menos buscas, mais leituras
    leituras: int = 15
    passos_usados: int = 0
    buscas_usadas: int = 0
    leituras_usadas: int = 0

    def texto(self) -> str:
        return (
            f"[orçamento] passo {self.passos_usados}/{self.passos} · buscas {self.buscas_usadas}/{self.buscas}"
            f" · leituras {self.leituras_usadas}/{self.leituras}"
        )


@dataclass
class RelatorioPesquisa:
    """O que o ciclo devolve: o resultado (só achados VÁLIDOS) e o registro dos passos, para depuração."""

    resultado: ResultadoPesquisa
    passos: list[dict]
    paginas_lidas: dict[str, PaginaLida]  # SÓ as lidas com sucesso (as únicas que valem como prova)
    orcamento: Orcamento
    paginas_com_erro: dict[str, str] = field(default_factory=dict)  # {url: motivo da falha}

    def como_json(self) -> str:
        dados = {
            "resultado": self.resultado.model_dump(),
            "orcamento": asdict(self.orcamento),
            "paginas_lidas": list(self.paginas_lidas),
            "paginas_com_erro": self.paginas_com_erro,
            "passos": self.passos,
        }
        return json.dumps(dados, ensure_ascii=False, indent=2)


# =====================================================================================================================
# Utilidades pequenas
# =====================================================================================================================
# =====================================================================================================================
# O ciclo
# =====================================================================================================================
class _Ciclo:
    """Guarda o estado de UMA pesquisa. Classe (e não funções soltas) porque o estado é grande e compartilhado."""

    def __init__(self, pedido: PedidoComponente, orcamento: Orcamento, cancelar: Callable[[], bool] | None,
                 modelo: str, esforco: str | None, timeout: float,
                 ao_evento: Callable[[EventoDeProgresso], None] | None = None):
        self.pedido = pedido
        self.ao_evento, self.inicio = ao_evento, time.monotonic()  # acompanhamento opcional (ver `progresso.py`)
        self.orc = orcamento
        self.cancelar = cancelar or (lambda: False)
        self.modelo, self.esforco, self.timeout = modelo, esforco, timeout
        self.mensagens: list[dict] = [
            {"role": "system", "content": PROMPT_SISTEMA},
            {"role": "user", "content": "PEDIDO (JSON):\n" + pedido.model_dump_json(indent=2)},
        ]
        self.passos: list[dict] = []
        self.paginas: dict[str, PaginaLida] = {}  # {url: PaginaLida} SÓ das lidas com sucesso: a prova da conferência
        self.falhas: dict[str, str] = {}  # {url: erro} das leituras que falharam (não valem como prova)
        self.registro_invalido = False  # o último registrar_resultado foi recusado pela validação?
        self.repeticao_de_erro = ("", 0)  # (classe do último erro de registro, quantas vezes SEGUIDAS)
        # `self.alertas` guarda só alertas de PROCESSO (corte por tokens, orçamento, falha da LLM...). Os da
        # conferência ficam por componente e valem só os da conferência que VALEU para ele: um alerta de uma tentativa
        # recusada e depois corrigida não pode aparecer ao lado do componente já válido.
        self.alertas_por_mpn: dict[str, list[str]] = {}
        self.consultas: set[str] = set()  # consultas já feitas, normalizadas
        self.validos: dict[str, AchadoComponente] = {}  # chave: MPN normalizado (o mesmo achado não entra 2x)
        self.alertas: list[str] = []
        self.recusados: dict[str, AchadoRecusado] = {}  # chave: MPN normalizado; sai daqui se depois passar
        self.resumo_da_llm = ""  # texto livre da LLM: guardado, mas NÃO conferido
        self.revisao_usada = False
        self.encerrado = False
        self.cancelado = False
        # O "foco" diz ao Tavily que trechos da página interessam: a categoria e os parâmetros pedidos.
        partes = [pedido.categoria, *(r.parametro for r in pedido.requisitos), pedido.encapsulamento or ""]
        self.foco = ", ".join(p for p in partes if p)[: tavily_client.TAMANHO_MAXIMO_CONSULTA]

    # ----------------------------------------------------------------------------------------------------------------
    def executar(self) -> RelatorioPesquisa:
        while not self.encerrado and self.orc.passos_usados < self.orc.passos:
            if self.cancelar():
                self._marcar_cancelado()
                break
            self.orc.passos_usados += 1
            self.mensagens.append({"role": "user", "content": self.orc.texto()})  # regra 1: orçamento a cada passo
            if not self._passo(tool_choice="auto"):
                break

        # Regra 3: o orçamento acabou sem registro definitivo -> uma última chamada que OBRIGA o registro.
        if not self.encerrado and not self.cancelado and self.orc.passos_usados >= self.orc.passos:
            self.alertas.append("orçamento de passos esgotado: registro forçado")
            self._evento("aviso", "Orçamento de passos esgotado: a LLM precisa registrar o que já tem")
            aviso = "O orçamento acabou. Chame registrar_resultado AGORA, com definitivo=true e o que você já tem."
            self.mensagens.append({"role": "user", "content": aviso})
            self.registro_invalido = False
            continuar = self._passo(tool_choice=_FORCAR_REGISTRO, final=True)
            # Registro forçado INVÁLIDO (ex.: evidência sem citação): UMA chamada extra, fora do orçamento, para a LLM
            # corrigir com a mensagem de erro (que já está no histórico, como resultado da ferramenta).
            if continuar and not self.encerrado and self.registro_invalido:
                self.alertas.append("registro forçado inválido: uma chamada extra de correção")
                aviso = "O registro veio inválido (veja o erro acima). Corrija e chame registrar_resultado de novo."
                self.mensagens.append({"role": "user", "content": aviso})
                self._passo(tool_choice=_FORCAR_REGISTRO, final=True)
            if not self.encerrado:
                self.alertas.append("a LLM não registrou no passo final")
        return self._relatorio()

    def _marcar_cancelado(self) -> None:
        self.cancelado = True
        self.alertas.append("pesquisa cancelada pelo usuário")
        self._evento("aviso", "Pesquisa cancelada: devolvendo o que já foi conferido")

    def _evento(self, tipo: str, texto: str) -> None:
        """Avisa quem acompanha (se alguém acompanha). Um callback com defeito nunca quebra a pesquisa."""
        emitir(self.ao_evento, tipo, texto, segundos=time.monotonic() - self.inicio, alertas=self.alertas)

    def _chamar_llm(self, tool_choice) -> tg.RespostaConversa:
        """A ÚNICA chamada a `conversar` do ciclo, com as opções do agente (modelo, temperatura, tokens, tempo, esforço)."""
        opcoes: dict = {"modelo": self.modelo, "temperatura": TEMPERATURA_AGENTE, "max_tokens": MAX_TOKENS_AGENTE,
                        "timeout": self.timeout}
        if self.esforco:  # `conversar` não tem um argumento próprio: vai em `parametros_extras`, repassado à API
            opcoes["parametros_extras"] = {"reasoning_effort": self.esforco}
        return tg.conversar(self.mensagens, ferramentas=FERRAMENTAS, tool_choice=tool_choice, **opcoes)

    def _passo(self, *, tool_choice, final: bool = False) -> bool:
        """Uma chamada à LLM e a execução das ferramentas pedidas. Devolve False se o ciclo deve parar."""
        numero = len(self.passos) + 1
        cortes = 0
        gasto_da_cortada = {"tokens_entrada": 0, "tokens_saida": 0, "tokens_raciocinio": 0, "tokens_em_cache": 0}
        quando = "Passo extra (registro obrigatório)" if final else f"Passo {self.orc.passos_usados} de {self.orc.passos}"
        self._evento("passo", f"{quando}: aguardando a LLM")  # a espera mais longa: avisamos ANTES
        try:
            resposta = self._chamar_llm(tool_choice)
            # finish_reason "length": a resposta acabou no MEIO (o raciocínio gastou os tokens). O texto ou o JSON da
            # ferramenta vieram pela metade, então NÃO entram no histórico. Pedimos brevidade e tentamos UMA vez mais.
            if resposta.motivo_parada == "length":
                cortes = 1
                # A tentativa cortada também foi COBRADA: seus tokens entram no registro do passo.
                gasto_da_cortada = {campo: getattr(resposta, campo) for campo in gasto_da_cortada}
                self.alertas.append(f"passo {numero}: resposta cortada por limite de tokens; nova tentativa")
                self._evento("aviso", "Resposta cortada por limite de tokens; pedindo de novo, mais curta")
                self.mensagens.append({"role": "user", "content": _AVISO_CORTE})
                resposta = self._chamar_llm(tool_choice)
                if resposta.motivo_parada == "length":
                    cortes = 2
        except tg.ErroTogether as erro:  # mensagem já escrita para o usuário, sem dados sensíveis
            self.alertas.append(f"{PREFIXO_FALHA_LLM}: {erro}")
            self._evento("aviso", f"Falha ao falar com a LLM: {erro}")
            return False

        registro = {
            "passo": numero,
            "forcado": final,
            "cortes_por_tokens": cortes,
            "texto": resposta.texto,  # COMPLETO, como os argumentos e resultados abaixo (o registro reconstrói a conversa)
            # Soma das tentativas do passo (a cortada, se houve, e a última).
            **{campo: gasto + getattr(resposta, campo) for campo, gasto in gasto_da_cortada.items()},
            "ferramentas": [],
        }
        self.passos.append(registro)
        if cortes == 2:  # cortada de novo: descartamos o passo, mas o ciclo segue (o orçamento limita as tentativas)
            self.alertas.append(f"passo {numero}: cortada de novo por limite de tokens; passo descartado")
            return True
        self.mensagens.append(resposta.como_mensagem())  # o pedido da LLM entra no histórico ANTES dos resultados
        if resposta.texto.strip():  # só a FALA curta; o raciocínio interno (`resposta.raciocinio`) não é mostrado
            self._evento("fala_da_llm", resposta.texto)

        if not resposta.chamadas:
            lembrete = "Responda usando as ferramentas: buscar_na_web, ler_pagina ou registrar_resultado."
            self.mensagens.append({"role": "user", "content": lembrete})
            return True

        for chamada in resposta.chamadas:
            if self.encerrado:
                break  # o registro definitivo já encerrou a pesquisa; o resto não importa
            if self.cancelar():  # regra 5: o cancelamento também é checado entre uma ferramenta e outra
                self._marcar_cancelado()
                return False
            conteudo = self._executar_ferramenta(chamada, final)
            registro["ferramentas"].append(
                {"nome": chamada.nome, "argumentos": chamada.argumentos_json, "resultado": conteudo}
            )
            self.mensagens.append(tg.mensagem_resultado(chamada, conteudo))
        return True

    def _executar_ferramenta(self, chamada: tg.ChamadaFerramenta, final: bool) -> str:
        """Despacha para a ferramenta pedida. Nunca levanta erro: todo problema vira texto para a LLM corrigir."""
        if final and chamada.nome != "registrar_resultado":
            return "Recusado: o orçamento acabou; só registrar_resultado é aceito."
        acoes = {"buscar_na_web": self._buscar, "ler_pagina": self._ler, "registrar_resultado": self._registrar}
        acao = acoes.get(chamada.nome)
        if acao is None:
            return f"Erro: ferramenta desconhecida ({chamada.nome[:40]})."
        try:
            return acao(chamada.argumentos_json, final)
        except ValidationError as erro:  # JSON malformado ou campos errados (o Pydantic cobre os dois casos)
            self.registro_invalido = chamada.nome == "registrar_resultado"
            if not self.registro_invalido:
                return "Erro: argumentos inválidos. Corrija e chame de novo.\n" + erros_curtos(erro)
            # O MESMO tipo de erro de novo? A mensagem fica mais firme (e diz qual tentativa é).
            classe, vezes = classe_do_erro(erro), self.repeticao_de_erro[1]
            self.repeticao_de_erro = (classe, vezes + 1 if classe == self.repeticao_de_erro[0] else 1)
            falta = "falta o campo `citacao`" if classe == "citacao" else "argumentos inválidos"
            self._evento("correcao", f"Registro recusado pela validação ({falta}). A LLM vai corrigir")
            return mensagem_de_registro_invalido(erro, self.repeticao_de_erro[1])
        except tavily_client.ErroTavily as erro:
            self._evento("aviso", f"Erro do Tavily: {erro}")
            return f"Erro na ferramenta: {erro}"

    # ----------------------------------------------------------------------------------------------------------------
    # Ferramentas
    # ----------------------------------------------------------------------------------------------------------------
    def _buscar(self, argumentos: str, _final: bool) -> str:
        consulta = ArgsBusca.model_validate_json(argumentos).consulta
        chave = normalizar_texto(consulta)  # "LDO 3.3V" e "  ldo 3.3v " são a mesma consulta
        if chave in self.consultas:  # regra 2
            self._evento("aviso", f"Consulta repetida recusada: {consulta}")
            return "Recusado: consulta repetida. Use outros termos, outro fabricante ou outro idioma."
        if self.orc.buscas_usadas >= self.orc.buscas:
            return "Recusado: orçamento de buscas esgotado. Leia páginas já encontradas ou registre."
        resultados = tavily_client.buscar(
            consulta, max_resultados=MAX_RESULTADOS_BUSCA, excluir_dominios=list(REDES_SOCIAIS)
        )
        self.consultas.add(chave)  # só conta depois de dar certo: uma falha de rede não "queima" a consulta
        self.orc.buscas_usadas += 1
        self._evento("busca", f"{consulta} ({len(resultados)} resultado(s))")
        # Filtramos de novo aqui (defesa em profundidade): se o filtro do Tavily falhar, a rede social não passa.
        itens = [
            {"titulo": r.titulo[:200], "url": r.url, "trecho": r.conteudo[:MAX_TRECHO_RESULTADO]}
            for r in resultados
            if not _rede_social(r.url)
        ]
        return _como_dado("resultados", itens)

    def _ler(self, argumentos: str, _final: bool) -> str:
        url = ArgsLeitura.model_validate_json(argumentos).url
        if _rede_social(url):
            return "Recusado: redes sociais não são fontes aceitas."
        chave = normalizar_url(url)
        if chave is None:
            return "Erro: a url precisa começar com http:// ou https://."
        # Já tentada? Devolvemos o mesmo resultado (página ou erro), sem gastar orçamento (nem créditos).
        pagina = next((p for u, p in self.paginas.items() if normalizar_url(u) == chave), None)
        erro = next((e for u, e in self.falhas.items() if normalizar_url(u) == chave), None)
        if pagina is None and erro is None:
            if self.orc.leituras_usadas >= self.orc.leituras:
                return "Recusado: orçamento de leituras esgotado. Registre com o que já foi lido."
            self._evento("leitura", url)  # ANTES: a leitura pode levar dezenas de segundos
            lida = tavily_client.extrair([url], foco=self.foco)[0]
            self.orc.leituras_usadas += 1
            if lida.erro or not lida.conteudo:  # falhou ("Failed to fetch url") ou veio vazia: NÃO é página lida
                erro = self.falhas[url] = (lida.erro or "a página veio sem texto")[:200]
                self._evento("leitura_falhou", f"{url} ({erro})")
            else:
                # Guardamos o texto TRUNCADO: a conferência só aceita como prova o que a LLM de fato viu.
                pagina = self.paginas[url] = lida.model_copy(update={"conteudo": lida.conteudo[:MAX_CARACTERES_PAGINA]})
        if erro is not None:
            return _como_dado("erro_na_leitura", erro + " (esta url não vale como prova; tente outra)")
        return _como_dado("pagina", {"url": url, "conteudo": pagina.conteudo})

    def _registrar(self, argumentos: str, final: bool) -> str:
        dados = ArgsRegistro.model_validate_json(argumentos)  # inválido -> ValidationError -> a LLM corrige
        self.repeticao_de_erro = ("", 0)  # registro válido: a contagem de erros seguidos recomeça
        if dados.resumo:
            self.resumo_da_llm = dados.resumo  # o argumento da ferramenta se chama `resumo`; aqui guardamos com o rótulo certo

        # Regra 4: TODO registro passa pela conferência determinística.
        conferencia = conferir_achados(dados.achados, self.paginas, self.pedido)
        recusados = [c for c in conferencia.achados if c.situacao == "recusado"]
        for conferido in conferencia.achados:
            chave = normalizar_texto(conferido.achado.mpn)
            if conferido.situacao == "valido":
                self.validos[chave] = conferido.achado  # já com evidências corrigidas
                self.recusados.pop(chave, None)  # recusado antes e corrigido na revisão: não é mais recusado
                self.alertas_por_mpn[chave] = conferido.alertas  # substitui os da tentativa anterior
            elif chave not in self.validos:
                self.alertas_por_mpn[chave] = conferido.alertas
                self.recusados[chave] = AchadoRecusado(
                    fabricante=conferido.achado.fabricante, mpn=conferido.achado.mpn, motivo=conferido.motivo[:MAX_MOTIVO]
                )

        faltam = MIN_ACHADOS_VALIDOS - len(self.validos)
        self._evento("registro", f"Registro recebido: {len(conferencia.achados) - len(recusados)} válido(s), "
                                 f"{len(recusados)} recusado(s)")
        if final or (dados.definitivo and (faltam <= 0 or self.revisao_usada)):
            self.encerrado = True
            return "Registro final recebido."
        for c in recusados:  # a LLM ainda vai ver a conferência e poder corrigir
            self._evento("correcao", f"A conferência recusou {c.achado.mpn}: {c.motivo}. A LLM vai corrigir")
        if dados.definitivo:  # faltam válidos e a revisão ainda não foi usada
            self.revisao_usada = True
            texto = (
                f"Conferência: {len(self.validos)} achado(s) válido(s); faltam {faltam}. Você tem UMA chance de revisar:"
                " corrija os recusados ou encontre outros, e registre de novo com definitivo=true."
            )
        else:
            texto = f"Registro parcial conferido: {len(self.validos)} achado(s) válido(s) até agora."
        if recusados:
            texto += "\nRecusados:\n" + "\n".join(
                f"- {c.achado.fabricante[:40]} {c.achado.mpn[:40]}: {c.motivo}" for c in recusados
            )
        if conferencia.alertas:
            texto += "\nAlertas da conferência:\n" + "\n".join(f"- {a}" for a in conferencia.alertas)
        return texto

    # ----------------------------------------------------------------------------------------------------------------
    def _relatorio(self) -> RelatorioPesquisa:
        validos = list(self.validos.values())[:MAX_ACHADOS]
        recusados = list(self.recusados.values())[:MAX_RECUSADOS]
        # O comentário da LLM ainda cita um componente que o código recusou? Avisamos (ele não vira o resumo).
        self.alertas.extend(alertas_do_comentario(self.resumo_da_llm, recusados))
        if len(self.validos) < MIN_ACHADOS_VALIDOS and not self.cancelado:
            self.alertas.append(f"apenas {len(self.validos)} achado(s) válido(s) (meta: {MIN_ACHADOS_VALIDOS})")
        # Processo + os da conferência que valeu para cada componente do resultado (válidos e recusados, nessa ordem).
        da_conferencia = [a for chave in [*self.validos, *self.recusados] for a in self.alertas_por_mpn.get(chave, [])]
        # Sem repetidos, cada um com no máximo MAX_ALERTA caracteres e no máximo MAX_ALERTAS (limites do modelo).
        alertas = list(dict.fromkeys(a[:MAX_ALERTA] for a in [*self.alertas, *da_conferencia]))
        if len(alertas) > MAX_ALERTAS:
            alertas = alertas[: MAX_ALERTAS - 1] + [f"... e mais {len(alertas) - MAX_ALERTAS + 1} alerta(s)"]
        resultado = ResultadoPesquisa(
            achados=validos, recusados=recusados, alertas=alertas, resumo_da_llm=self.resumo_da_llm,
            resumo_conferido=montar_resumo_conferido(validos, recusados),  # escrito pelo CÓDIGO: bate com o resultado
        )
        return RelatorioPesquisa(resultado=resultado, passos=self.passos, paginas_lidas=self.paginas,
                                 orcamento=self.orc, paginas_com_erro=self.falhas)


def pesquisar(
    pedido: PedidoComponente,
    *,
    orcamento: Orcamento | None = None,
    cancelar: Callable[[], bool] | None = None,
    modelo: str = MODELO_AGENTE_PESQUISA,
    esforco: str | None = None,
    timeout: float = TIMEOUT_CHAMADA_SEGUNDOS,
    ao_evento: Callable[[EventoDeProgresso], None] | None = None,
) -> RelatorioPesquisa:
    """Roda uma pesquisa completa para o `pedido`.

    Args:
        orcamento: limites de passos, buscas e leituras (padrão: 15, 10 e 15). É atualizado com o uso.
        cancelar: função chamada antes de cada passo e de cada ferramenta; se devolver True, a pesquisa para e
            devolve o que já tiver (ex.: ligada a um botão "Cancelar" no Streamlit).
        modelo: ID do modelo na Together (padrão: `MODELO_AGENTE_PESQUISA`).
        esforco: esforço de raciocínio ("low", "high" ou "max"); None não envia nada e vale o padrão do modelo.
        timeout: segundos de espera por CADA chamada à LLM. Esgotado, vira alerta e volta o resultado parcial.
        ao_evento: recebe um `EventoDeProgresso` a cada passo, busca, leitura, registro e aviso (ver `progresso.py`).

    Raises:
        ValueError: `esforco` inválido ou `timeout` <= 0 (erro de quem chama: falha antes de gastar créditos).
        Não levanta erro por falha da LLM ou do Tavily (nem tempo esgotado): isso vira alerta no resultado.
    """
    if esforco is not None and esforco not in ESFORCOS_ACEITOS:
        raise ValueError(f"esforco deve ser um de {ESFORCOS_ACEITOS} (ou None), não {esforco!r}")
    if timeout <= 0:
        raise ValueError("timeout precisa ser maior que zero")
    return _Ciclo(pedido, orcamento or Orcamento(), cancelar, modelo, esforco, timeout, ao_evento).executar()
