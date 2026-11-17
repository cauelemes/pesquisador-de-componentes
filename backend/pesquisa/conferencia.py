"""Conferência determinística dos achados: "a LLM julga, o código confere".

A LLM lê as páginas e diz "este componente atende, e a prova é esta frase". Aqui o código confere, sem LLM, se essa
prova existe de verdade e se diz o que a LLM afirma. As regras:

1. A citação de cada evidência precisa aparecer no texto da página lida (comparação tolerante a maiúsculas, acentos,
   tipos de espaço e de hífen; e, se preciso, só letras e dígitos, para PDFs com "Cur-rent"). A LLM pode juntar trechos
   com "..." : cada fragmento (de 12+ caracteres) precisa aparecer. Se não, "confirmado" cai para "indicio".
   Evidência de "encapsulamento" é conferida contra o `encapsulamento` do pedido; de "ciclo de vida" é descartada
   (o ciclo de vida é um campo do achado, não uma evidência).
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


MIN_FRAGMENTO = 12  # fragmento de citação mais curto que isso ("3.3 V") casaria por acaso: não conta como prova
_RETICENCIAS = re.compile(r"\.{3,}")  # o NFKD da normalização já transforma "…" em "...": um separador cobre os dois
# Nomes (normalizados) com que a LLM chama as evidências que NÃO são requisitos do pedido.
NOMES_ENCAPSULAMENTO = {"encapsulamento", "package", "pacote"}
NOMES_CICLO_DE_VIDA = {"ciclo de vida", "ciclo_de_vida", "lifecycle", "life cycle"}


def _alfanumerico(texto: str) -> str:
    """Letras, dígitos e o separador decimal, já normalizado. Serve de SEGUNDA tentativa na comparação: resolve
    hifenização de fim de linha e espaçamentos estranhos de PDFs, SEM juntar números diferentes.

    "Cur-\\nrent" -> "current"      "SOT-223-4" -> "sot2234"      "up to1A" -> "upto1a"
    "3,3 V" e "3.3V" -> "3.3v" (casam entre si)      "33 V" -> "33v" (NÃO casa com "3.3v")

    Por que manter o decimal: sem ele, "3.3 V" e "33 V" viravam "33v", e uma citação com o valor ERRADO passaria
    na conferência, que existe justamente para barrar isso. Um "." ou "," só é decimal se estiver ENTRE dois dígitos
    (olhando o texto antes da filtragem); o ponto final de "fim. 3" não fica.
    """
    texto = normalizar_texto(texto)
    saida = []
    for i, c in enumerate(texto):
        if c.isalnum():
            saida.append(c)
        elif c in ".," and 0 < i < len(texto) - 1 and texto[i - 1].isdigit() and texto[i + 1].isdigit():
            saida.append(".")  # vírgula ou ponto decimal: um só caractere, para "3,3" casar com "3.3"
    return "".join(saida)


def _numero_continua(texto: str, i: int, passo: int) -> bool:
    """Na posição `i` do texto, um número "continua"? Isto é: há um dígito, ou um separador decimal (. ou ,) colado a
    um dígito logo adiante. `passo` = -1 olha para trás (antes do casamento), +1 olha para frente (depois)."""
    if not 0 <= i < len(texto):
        return False  # começo ou fim do texto: nada continua
    if texto[i].isdigit():
        return True
    vizinho = i + passo
    return texto[i] in ".," and 0 <= vizinho < len(texto) and texto[vizinho].isdigit()


def _aparece_com_bordas(fragmento: str, texto: str) -> bool:
    """O fragmento aparece no texto SEM estar no meio de outro número?

    Se o fragmento começa com dígito, o que vem antes dele na página não pode continuar um número: "3.3 v output"
    não vale dentro de "13.3 v output" nem de "2.3.3 v output". O mesmo vale para o fim: "is 1.5" não vale dentro de
    "is 1.55 a". Basta UMA ocorrência com bordas válidas (a primeira pode estar dentro de outro número e a segunda não).
    """
    inicio = 0
    while (i := texto.find(fragmento, inicio)) != -1:  # `:=` (morsa): atribui e testa na mesma linha
        fim = i + len(fragmento)
        borda_antes_ok = not (fragmento[0].isdigit() and _numero_continua(texto, i - 1, -1))
        borda_depois_ok = not (fragmento[-1].isdigit() and _numero_continua(texto, fim, +1))
        if borda_antes_ok and borda_depois_ok:
            return True
        inicio = i + 1  # procura a próxima ocorrência
    return False


def citacao_confere(citacao: str, texto_normalizado: str, texto_alfanumerico: str) -> str:
    """Confere uma citação contra a página. Devolve "ok", "curta" (nada para conferir) ou "ausente".

    1. Divide a citação nos "..." (a LLM junta trechos separados da página numa citação só).
    2. Descarta fragmentos com menos de MIN_FRAGMENTO caracteres: "3.3 V" sozinho apareceria por acaso.
    3. Cada fragmento restante precisa aparecer: primeiro com a normalização comum; senão, na forma de reserva
       (`_alfanumerico`). Nas duas, sem casar no meio de outro número (`_aparece_com_bordas`).
    4. Vale só se TODOS aparecem, e se sobrou pelo menos um fragmento.
    """
    fragmentos = [f.strip() for f in _RETICENCIAS.split(normalizar_texto(citacao))]
    fragmentos = [f for f in fragmentos if len(f) >= MIN_FRAGMENTO]
    if not fragmentos:
        return "curta"
    for fragmento in fragmentos:
        reserva = _alfanumerico(fragmento)
        if not (_aparece_com_bordas(fragmento, texto_normalizado) or
                (reserva and _aparece_com_bordas(reserva, texto_alfanumerico))):
            return "ausente"
    return "ok"


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
    candidatas = [(valor, unidade) for valor, unidade in quantidades_do_texto(_RETICENCIAS.sub(" ", evidencia.citacao))
                  if unidade == base]
    if not candidatas:
        return "indicio", f"citação sem quantidade em {base}"
    if any(atende(requisito, valor, unidade) for valor, unidade in candidatas):
        return "confirmado", None  # basta uma: a frase pode ter mínimo e máximo ("4.5 V to 28 V")
    return "contradiz", "nenhum valor da citação atende ao requisito"


def _conferir_evidencia(
    evidencia: EvidenciaRequisito,
    textos: tuple[str, str],
    requisitos: dict[str, Requisito],
    encapsulamento: str | None,
) -> tuple[EvidenciaRequisito, str | None]:
    """Regras 1 e 3 para UMA evidência. `textos` = página (normalizada, só letras e dígitos).
    Devolve (evidência corrigida, alerta ou None)."""
    nome = _curto(evidencia.requisito)

    # Regra 1: citação que não está na página. "confirmado" cai para "indicio"; nas outras situações, só avisamos.
    if evidencia.citacao:
        resultado = citacao_confere(evidencia.citacao, *textos)
        if resultado != "ok":
            nova = "indicio" if evidencia.situacao == "confirmado" else evidencia.situacao
            problema = "citação não encontrada na página" if resultado == "ausente" else "citação curta demais"
            return evidencia.model_copy(update={"situacao": nova}), f"{nome}: {problema}"

    if evidencia.situacao != "confirmado":
        return evidencia, None  # indicio/nao_encontrado/contradiz não são promovidos nem conferidos aqui
    # (Daqui em diante a citação existe e foi achada: "confirmado" sem citação já é recusado pelo modelo Pydantic.)

    requisito = requisitos.get(normalizar_texto(evidencia.requisito))
    if requisito is None:
        # Encapsulamento: não é requisito, é um campo à parte do pedido. Checagem implícita: a citação (só letras e
        # dígitos) contém o encapsulamento pedido. "SOT-223" casa com "SOT223" e com "SOT-223-4".
        if encapsulamento and normalizar_texto(evidencia.requisito) in NOMES_ENCAPSULAMENTO:
            if _alfanumerico(encapsulamento) in _alfanumerico(evidencia.citacao):
                return evidencia, None
            alerta = f"{nome}: citação não menciona {_curto(encapsulamento)}; confirmado -> indicio"
            return evidencia.model_copy(update={"situacao": "indicio"}), alerta
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
    # O ciclo de vida é um CAMPO do achado (regra 5), nunca uma evidência: a evidência é descartada, sem alerta.
    sem_ciclo = [e for e in achado.evidencias if normalizar_texto(e.requisito) not in NOMES_CICLO_DE_VIDA]
    achado = achado.model_copy(update={"evidencias": sem_ciclo})

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
    textos = (texto, _alfanumerico(texto))

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
        corrigida, alerta = _conferir_evidencia(evidencia, textos, requisitos, pedido.encapsulamento)
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
