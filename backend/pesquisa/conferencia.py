"""Conferência determinística dos achados: "a LLM julga, o código confere".

A LLM lê as páginas e diz "este componente atende, e a prova é esta frase". Aqui o código confere, sem LLM, se essa
prova existe de verdade e se diz o que a LLM afirma. As regras:

1. A citação de cada evidência precisa aparecer no texto da página lida (comparação tolerante a maiúsculas, acentos,
   tipos de espaço e de hífen). Se não aparecer, "confirmado" cai para "indicio".
2. O MPN (código da peça) precisa aparecer na página. Se só a família aparecer (`raiz_do_mpn`: "AMS1117" de
   "AMS1117-3.3"), o achado continua válido, com alerta. Se nem a família aparecer, o achado é RECUSADO.
3. Os números da citação precisam atender ao requisito. Se a LLM disse "confirmado", mas nenhuma quantidade da citação
   atende, vira "contradiz" (ou "indicio", se a citação não tiver nenhuma quantidade na unidade do requisito).
4. A URL do achado precisa estar entre as páginas efetivamente lidas. Se não, o achado é RECUSADO.
5. Ciclo de vida "obsoleto" ou "nrnd": o achado é RECUSADO. "desconhecido": só um alerta (continua válido).

Nada aqui chama rede ou LLM: dá para testar tudo com dados fixos.

ATENÇÃO: os alertas incluem trechos vindos da LLM (MPN, nome do requisito). São DADO: mostre com `st.text`.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from ..compartilhado.modelos import AchadoComponente, EvidenciaRequisito, PaginaLida, PedidoComponente, Requisito
from ..compartilhado.unidades import ErroDeUnidade, atende, quantidades_do_texto, unidade_base
from .urls import normalizar_url

# Todos os tipos de hífen e travessão que aparecem em datasheets e páginas, para virarem um "-" comum:
# ‐ hífen, ‑ hífen não separável, ‒ traço numérico, – meia-risca, — travessão, ― barra horizontal, − sinal de menos,
# ﹣ e － (versões "pequena" e "larga").
_HIFENS = re.compile("[‐‑‒–—―−﹣－]")
_ESPACOS = re.compile(r"\s+")  # em Python, `\s` já inclui o espaço não separável (U+00A0), o espaço fino etc.
_TAMANHO_NO_ALERTA = 40  # trechos da LLM dentro de um alerta são cortados: alerta é curto


# =====================================================================================================================
# O que a conferência devolve
# =====================================================================================================================
# `dataclass` e não Pydantic: isto é SAÍDA do nosso código, não entrada externa. Os textos externos que estão aqui
# dentro (o achado) já passaram pelo Pydantic antes.


@dataclass(frozen=True)  # frozen: depois de criado, não muda (ninguém "conserta" um resultado sem querer)
class AchadoConferido:
    """Um achado depois da conferência, com as evidências já corrigidas."""

    achado: AchadoComponente
    situacao: Literal["valido", "recusado"]
    motivo: str = ""  # por que foi recusado ("" se válido); vários motivos vêm separados por "; "
    alertas: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResultadoConferencia:
    """A conferência de vários achados, com todos os alertas juntos."""

    achados: list[AchadoConferido]
    alertas: list[str]


# =====================================================================================================================
# Normalização de texto (regras 1 e 2)
# =====================================================================================================================


def normalizar_texto(texto: str) -> str:
    """Deixa o texto num formato padrão, para comparar "a mesma frase" escrita de jeitos ligeiramente diferentes.

    "Tensão – 3,3 V" e "tensao - 3,3 v" ficam iguais. Os passos:
    - NFKD separa letra e acento ("ã" -> "a" + "~"), e depois jogamos fora os acentos (os "combining").
      O NFKD também converte "µ" (micro) em "μ" (mu grego) e o espaço não separável em espaço comum.
    - casefold: minúsculas (mais completo que `lower()` para outros alfabetos).
    - todos os hífens e travessões viram "-"; qualquer sequência de espaços vira um espaço só.
    """
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.casefold().replace("µ", "μ")  # garantia extra para o micro, caso escape do NFKD
    texto = _HIFENS.sub("-", texto)
    return _ESPACOS.sub(" ", texto).strip()


def _aparece(trecho: str, texto_normalizado: str) -> bool:
    """O trecho aparece no texto (já normalizado)? Trecho vazio nunca "aparece": prova vazia não prova nada."""
    trecho = normalizar_texto(trecho)
    return bool(trecho) and trecho in texto_normalizado


MIN_RAIZ = 4  # raiz mais curta que isso casaria com qualquer coisa ("LM3" está em centenas de peças)
# Letras + dígitos + até 2 letras de série. Ex.: "NCP" + "1117" + "LP" de "NCP1117LPST33T3G".
_RAIZ_COM_SERIE = re.compile(r"[a-z]+[0-9]+[a-z]{0,2}")
_RAIZ_CURTA = re.compile(r"[a-z]+[0-9]+")  # só letras + dígitos. Ex.: "LM317" de "LM317T"


def raiz_do_mpn(mpn: str) -> str | None:
    """A "família" de um MPN (já normalizada), ou None se não houver uma raiz confiável.

    MPNs costumam ser família + variante (tensão, encapsulamento, embalagem). Regras, em ordem:
    1. Com hífen: o que vem antes do primeiro hífen.            "AMS1117-3.3"      -> "ams1117"
    2. Sem hífen: letras + dígitos + até 2 letras de série.     "NCP1117LPST33T3G" -> "ncp1117lp"
    3. Se a regra 2 pegou o MPN inteiro: só letras + dígitos.   "LM317T"           -> "lm317"
    Sem raiz (None): menos de MIN_RAIZ caracteres, igual ao próprio MPN ou começando por dígito ("1N4007").
    """
    mpn = normalizar_texto(mpn)
    if "-" in mpn:
        raiz = mpn.split("-", 1)[0]
    else:
        casamento = _RAIZ_COM_SERIE.match(mpn)
        raiz = casamento.group() if casamento else ""
        if raiz == mpn:
            casamento = _RAIZ_CURTA.match(mpn)
            raiz = casamento.group() if casamento else ""
    return raiz if len(raiz) >= MIN_RAIZ and raiz != mpn else None


def _familia_aparece(raiz: str, texto_normalizado: str) -> bool:
    """A raiz aparece como começo de um código na página? Não pode vir colada a letra/dígito antes nem a DÍGITO depois:
    "lm317" casa com "lm317t" e "lm317 series", mas não com "lm3175" (outra peça) nem com "xlm317"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(raiz)}(?![0-9])", texto_normalizado) is not None


def _curto(texto: str) -> str:
    """Corta texto da LLM para caber num alerta."""
    return texto if len(texto) <= _TAMANHO_NO_ALERTA else texto[: _TAMANHO_NO_ALERTA - 1] + "…"


# =====================================================================================================================
# Regras
# =====================================================================================================================


def _pagina_lida(url: str, paginas_lidas: dict[str, PaginaLida]) -> PaginaLida | None:
    """Regra 4: acha a página do achado entre as lidas. Só conta página lida COM SUCESSO (sem erro e com texto).

    As URLs são comparadas normalizadas: "HTTPS://Site.com/x/" e "https://site.com/x" são a mesma página.
    """
    alvo = normalizar_url(url)
    if alvo is None:
        return None
    for url_lida, pagina in paginas_lidas.items():
        if normalizar_url(url_lida) == alvo and pagina.erro is None and pagina.conteudo:
            return pagina
    return None


def _conferir_numeros(evidencia: EvidenciaRequisito, requisito: Requisito) -> tuple[str, str | None]:
    """Regra 3: os números da citação atendem ao requisito? Devolve (nova situação, alerta ou None).

    Só é chamada para evidências "confirmado" cuja citação JÁ foi achada na página.
    """
    try:
        base = unidade_base(requisito.unidade)
    except ErroDeUnidade:
        return "indicio", f"unidade do requisito não reconhecida ({_curto(requisito.unidade)})"

    # Só interessam as quantidades na MESMA grandeza do requisito: se o requisito é em V, "3 A" na frase não conta.
    candidatas = [(valor, unidade) for valor, unidade in quantidades_do_texto(evidencia.citacao) if unidade == base]
    if not candidatas:
        return "indicio", f"citação sem quantidade em {base}"
    if any(atende(requisito, valor, unidade) for valor, unidade in candidatas):
        return "confirmado", None  # basta uma: a frase pode ter mínimo e máximo ("4.5 V to 28 V")
    return "contradiz", "nenhum valor da citação atende ao requisito"


def _conferir_evidencia(
    evidencia: EvidenciaRequisito, texto_da_pagina: str, requisitos: dict[str, Requisito]
) -> tuple[EvidenciaRequisito, str | None]:
    """Regras 1 e 3 para UMA evidência. Devolve (evidência corrigida, alerta ou None)."""
    nome = _curto(evidencia.requisito)
    citacao_achada = _aparece(evidencia.citacao, texto_da_pagina)

    # Regra 1: citação que não está na página. "confirmado" cai para "indicio"; nas outras situações, só avisamos.
    if not citacao_achada and evidencia.citacao:
        nova = "indicio" if evidencia.situacao == "confirmado" else evidencia.situacao
        return evidencia.model_copy(update={"situacao": nova}), f"{nome}: citação não encontrada na página"

    if evidencia.situacao != "confirmado":
        return evidencia, None  # indicio/nao_encontrado/contradiz não são promovidos nem conferidos aqui
    # (Daqui em diante a citação existe e foi achada: "confirmado" sem citação já é recusado pelo modelo Pydantic.)

    requisito = requisitos.get(normalizar_texto(evidencia.requisito))
    if requisito is None:
        return evidencia.model_copy(update={"situacao": "indicio"}), f"{nome}: requisito não existe no pedido"

    # Regra 3: a frase existe; agora, os números dela batem com o requisito?
    nova, problema = _conferir_numeros(evidencia, requisito)
    if problema is None:
        return evidencia, None
    # `model_copy(update=...)` cria uma CÓPIA com o campo trocado; o achado original da LLM fica intacto.
    return evidencia.model_copy(update={"situacao": nova}), f"{nome}: {problema}; confirmado -> {nova}"


def conferir_achado(
    achado: AchadoComponente, paginas_lidas: dict[str, PaginaLida], pedido: PedidoComponente
) -> AchadoConferido:
    """Aplica as 5 regras a um achado. Nunca levanta erro por causa do conteúdo: problemas viram alertas.

    Args:
        achado: o que a LLM afirmou.
        paginas_lidas: {url: PaginaLida} com as páginas que as ferramentas leram de fato.
        pedido: o pedido original; dele vêm os requisitos (operador, valor, unidade) para a regra 3.
    """
    mpn = _curto(achado.mpn)
    motivos: list[str] = []
    alertas: list[str] = []

    # Regra 4: sem a página lida não há como conferir nada. Recusamos já, e nenhuma evidência fica "confirmado".
    pagina = _pagina_lida(achado.url, paginas_lidas)
    if pagina is None:
        evidencias = [
            e.model_copy(update={"situacao": "indicio"}) if e.situacao == "confirmado" else e for e in achado.evidencias
        ]
        motivo = "URL não está entre as páginas lidas"
        return AchadoConferido(
            achado=achado.model_copy(update={"evidencias": evidencias}),
            situacao="recusado",
            motivo=motivo,
            alertas=[f"{mpn}: {motivo}"],
        )

    texto = normalizar_texto(pagina.conteudo)  # normalizamos a página UMA vez e reaproveitamos

    # Regra 2, em três níveis: (a) MPN exato na página: ok; (b) só a família (raiz) aparece: válido, com alerta;
    # (c) nem um nem outro: recusado. O nível (b) existe porque a leitura traz TRECHOS da página, e o código
    # completo (com sufixo de tensão/embalagem) muitas vezes só aparece numa tabela que não veio.
    if not _aparece(achado.mpn, texto):
        raiz = raiz_do_mpn(achado.mpn)
        if raiz and _familia_aparece(raiz, texto):
            alertas.append(f"{mpn}: MPN exato não encontrado na página; só a família ({raiz})")
        else:
            motivos.append("MPN não aparece na página")

    # Regra 5: obsoleto e nrnd saem da lista de válidos; desconhecido só gera alerta (é o caso comum: datasheets
    # raramente dizem "ativo", e recusar por isso descartaria quase tudo).
    if achado.ciclo_de_vida in ("obsoleto", "nrnd"):
        motivos.append(f"ciclo de vida: {achado.ciclo_de_vida}")
    elif achado.ciclo_de_vida == "desconhecido":
        alertas.append(f"{mpn}: ciclo de vida desconhecido")

    # Regras 1 e 3, evidência por evidência. Os requisitos são achados pelo nome normalizado ("Tensão" == "tensao").
    requisitos = {normalizar_texto(r.parametro): r for r in pedido.requisitos}
    evidencias = []
    for evidencia in achado.evidencias:
        corrigida, alerta = _conferir_evidencia(evidencia, texto, requisitos)
        evidencias.append(corrigida)
        if alerta:
            alertas.append(f"{mpn}: {alerta}")

    alertas = [f"{mpn}: {m}" for m in motivos] + alertas
    return AchadoConferido(
        achado=achado.model_copy(update={"evidencias": evidencias}),
        situacao="recusado" if motivos else "valido",
        motivo="; ".join(motivos),
        alertas=alertas,
    )


def conferir_achados(
    achados: list[AchadoComponente], paginas_lidas: dict[str, PaginaLida], pedido: PedidoComponente
) -> ResultadoConferencia:
    """Confere vários achados e junta os alertas de todos, na ordem."""
    conferidos = [conferir_achado(a, paginas_lidas, pedido) for a in achados]
    return ResultadoConferencia(achados=conferidos, alertas=[alerta for c in conferidos for alerta in c.alertas])
