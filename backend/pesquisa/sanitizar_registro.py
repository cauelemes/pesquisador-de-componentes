"""Recorta uma execução gravada para virar o exemplo PÚBLICO do modo demonstração.

    python -m backend.pesquisa.sanitizar_registro tmp_debugs/gravada.json exemplos/execucao_gravada.json

Por que recortar: o registro completo traz o texto de cada página lida (até 12 mil caracteres por datasheet de
terceiros). O exemplo público só precisa do necessário para PROVAR as citações. O recorte:
1. Em cada página, mantém só os trechos em volta das citações e das ocorrências dos MPNs (e da "família" do MPN),
   com MARGEM caracteres de cada lado, juntando trechos que se tocam, separados por " ... ".
2. Corta cada resultado de busca gravado a MAX_POR_RESULTADO_DE_BUSCA caracteres.
3. Apaga o que pareça caminho de arquivo local, e-mail ou chave de API (tgp_..., tvly-...).
4. VERIFICA: repete a execução sobre o registro recortado (pelo código real, via `demo.repetir`) e confere que os
   achados válidos (com as situações das evidências) e os recusados (com o motivo) são os MESMOS do original. Se não
   forem, falha com a lista das diferenças: um exemplo público que prova outra coisa seria pior que nenhum.
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from .conferencia import MIN_FRAGMENTO, _RETICENCIAS, normalizar_texto, raiz_do_mpn
from .demo import ErroDeReplay, repetir
from .ferramentas import _como_dado
from .registro import RegistroPesquisa, carregar_registro, registro_em_json, salvar_registro
from .urls import normalizar_url

MARGEM = 300  # caracteres mantidos de cada lado de uma ocorrência
MAX_POR_RESULTADO_DE_BUSCA = 600
MAX_OCORRENCIAS_POR_ALVO = 20  # um MPN que aparece 200 vezes não precisa de 200 trechos
SEPARADOR = " ... "
SEM_ALVO = 300  # página sem nada a provar: fica só o começo (vazia, o agente a trataria como leitura que falhou)
REMOVIDO = "[removido]"

# O que NÃO pode ir para um arquivo público. Os caminhos só valem no começo de uma palavra (não no meio de uma URL).
PADROES_SENSIVEIS = [
    re.compile(r"\b(?:tgp_|tvly-)[A-Za-z0-9_\-]+"),  # chaves da Together e do Tavily
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # e-mail
    re.compile(r"(?<![\w.:/-])(?:/home|/Users|/root)/[^\s\"'<>]+"),  # caminho local (Linux/macOS)
    re.compile(r"(?<![\w/])~/[^\s\"'<>]+"),
    re.compile(r"\b[A-Za-z]:\\[^\s\"'<>]+"),  # caminho local (Windows)
]


class ErroDeSanitizacao(ValueError):
    """O recorte mudaria o resultado da conferência (ou não pôde ser feito). A mensagem diz o quê."""


# =====================================================================================================================
# Mapas: posição no texto normalizado -> posição no texto ORIGINAL
# =====================================================================================================================
def _normalizar_com_mapa(texto: str) -> tuple[str, list[int]]:
    """O mesmo que `normalizar_texto`, caractere a caractere, guardando de onde veio cada caractere da saída.

    Assim, uma ocorrência achada no texto normalizado (onde a conferência procura) vira um trecho exato do original.
    """
    saida: list[str] = []
    mapa: list[int] = []
    for i, c in enumerate(texto):
        if c.isspace():
            if saida and saida[-1] != " ":  # várias quebras/espaços seguidos viram um espaço só
                saida.append(" ")
                mapa.append(i)
            continue
        decomposto = "".join(d for d in unicodedata.normalize("NFKD", c) if not unicodedata.combining(d))
        for d in decomposto.casefold().replace("µ", "μ"):
            saida.append(" " if d.isspace() else d)
            mapa.append(i)
    while saida and saida[-1] == " ":
        saida.pop()
        mapa.pop()
    normal = "".join(saida)
    normal = re.sub("[‐‑‒–—―−﹣－]", "-", normal)  # hífens: 1 para 1
    if normal != normalizar_texto(texto):  # garantia: o mapa só vale se a normalização for IDÊNTICA à da conferência
        raise ErroDeSanitizacao("a normalização com mapa divergiu da conferência (caractere incomum na página?)")
    return normal, mapa


def _alfanumerico_com_mapa(normal: str) -> tuple[str, list[int]]:
    """O mesmo que `conferencia._alfanumerico` (já a partir do texto normalizado), guardando as posições."""
    saida, mapa = [], []
    for i, c in enumerate(normal):
        if c.isalnum():
            saida.append(c)
            mapa.append(i)
        elif c in ".," and 0 < i < len(normal) - 1 and normal[i - 1].isdigit() and normal[i + 1].isdigit():
            saida.append(".")
            mapa.append(i)
    return "".join(saida), mapa


def _ocorrencias(alvo: str, texto: str) -> list[tuple[int, int]]:
    inicio, achadas = 0, []
    while len(achadas) < MAX_OCORRENCIAS_POR_ALVO and (i := texto.find(alvo, inicio)) != -1:
        achadas.append((i, i + len(alvo)))
        inicio = i + 1
    return achadas


# =====================================================================================================================
# Recorte de uma página
# =====================================================================================================================
def _intervalos(texto: str, alvos: set[str]) -> list[tuple[int, int]]:
    """Os trechos (no texto ORIGINAL) onde cada alvo aparece: primeiro na forma normalizada; se não, na de reserva."""
    normal, mapa = _normalizar_com_mapa(texto)
    alfa, mapa_alfa = _alfanumerico_com_mapa(normal)
    trechos = []
    for alvo in alvos:
        forma = normalizar_texto(alvo)
        achadas = [(mapa[a], mapa[b - 1] + 1) for a, b in _ocorrencias(forma, normal)] if forma else []
        if not achadas:
            reserva, _ = _alfanumerico_com_mapa(forma)
            if reserva:
                achadas = [(mapa[mapa_alfa[a]], mapa[mapa_alfa[b - 1]] + 1) for a, b in _ocorrencias(reserva, alfa)]
        trechos.extend(achadas)
    return trechos


def recortar_pagina(texto: str, alvos: set[str]) -> str:
    """Só os trechos em volta dos alvos (MARGEM de cada lado), juntando os que se tocam, separados por SEPARADOR."""
    trechos = sorted((max(a - MARGEM, 0), min(b + MARGEM, len(texto))) for a, b in _intervalos(texto, alvos))
    if not trechos:
        return texto[:SEM_ALVO]
    juntos = [list(trechos[0])]
    for a, b in trechos[1:]:
        if a <= juntos[-1][1]:  # sobrepõe ou encosta no anterior: um trecho só
            juntos[-1][1] = max(juntos[-1][1], b)
        else:
            juntos.append([a, b])
    return SEPARADOR.join(texto[a:b] for a, b in juntos)


# =====================================================================================================================
# O registro inteiro
# =====================================================================================================================
def _achados_propostos(registro: RegistroPesquisa) -> list[dict]:
    """Todos os achados que a LLM propôs em algum registro (válidos, recusados e tentativas anteriores)."""
    achados = [a.model_dump() for a in registro.resultado.achados]
    for passo in registro.passos:
        for f in passo.ferramentas:
            if f.nome != "registrar_resultado":
                continue
            try:
                dados = json.loads(f.argumentos)
            except ValueError:
                continue
            if isinstance(dados, dict) and isinstance(dados.get("achados"), list):
                achados.extend(a for a in dados["achados"] if isinstance(a, dict))
    return achados


def _alvos(registro: RegistroPesquisa) -> set[str]:
    """O que precisa sobreviver nas páginas: cada fragmento de citação, cada MPN e a família de cada MPN."""
    alvos: set[str] = set()
    for achado in _achados_propostos(registro):
        mpn = str(achado.get("mpn") or "")
        if mpn:
            alvos.add(mpn)
            if raiz := raiz_do_mpn(mpn):
                alvos.add(raiz)
        for evidencia in achado.get("evidencias") or []:
            citacao = str((evidencia or {}).get("citacao") or "")
            alvos.update(f.strip() for f in _RETICENCIAS.split(citacao) if len(normalizar_texto(f)) >= MIN_FRAGMENTO)
    return alvos


def _cortar_busca(resultado: str) -> str:
    """Um resultado de `buscar_na_web` como a LLM o recebeu, com cada item cortado a MAX_POR_RESULTADO_DE_BUSCA."""
    if not resultado.startswith("{"):
        return resultado  # "Recusado: ...", "Erro na ferramenta: ..."
    itens = []
    for item in json.loads(resultado).get("resultados", []):
        titulo, url = str(item.get("titulo", ""))[:200], str(item.get("url", ""))
        espaco = max(MAX_POR_RESULTADO_DE_BUSCA - len(titulo) - len(url), 0)
        itens.append({"titulo": titulo, "url": url, "trecho": str(item.get("trecho", ""))[:espaco]})
    return _como_dado("resultados", itens)


def _refazer_leitura(resultado: str, paginas: dict[str, str]) -> str:
    """O resultado de `ler_pagina` refeito com a página RECORTADA (o passo gravado não pode trazer a página inteira)."""
    if not resultado.startswith("{"):
        return resultado
    dados = json.loads(resultado)
    if "pagina" not in dados:
        return resultado  # erro de leitura: fica como está
    url = dados["pagina"]["url"]
    return _como_dado("pagina", {"url": url, "conteudo": paginas.get(normalizar_url(url), "")})


def _ocultar(valor):
    """Apaga, em qualquer texto (inclusive chaves de dicionário), o que parecer caminho local, e-mail ou chave."""
    if isinstance(valor, str):
        for padrao in PADROES_SENSIVEIS:
            valor = padrao.sub(REMOVIDO, valor)
        return valor
    if isinstance(valor, dict):
        return {_ocultar(k): _ocultar(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_ocultar(v) for v in valor]
    return valor


def _impressao_digital(resultado) -> tuple:
    """O que precisa ser IGUAL antes e depois: válidos (MPN e situação de cada evidência) e recusados (MPN e motivo)."""
    validos = sorted((a.mpn, tuple(e.situacao for e in a.evidencias)) for a in resultado.achados)
    recusados = sorted((r.mpn, r.motivo) for r in resultado.recusados)
    return validos, recusados


def sanitizar(registro: RegistroPesquisa) -> RegistroPesquisa:
    """Devolve o registro recortado e marcado `demonstracao=True`, já VERIFICADO (ver o topo do arquivo)."""
    alvos = _alvos(registro)
    paginas = {url: recortar_pagina(texto, alvos) for url, texto in registro.paginas.items()}
    por_url = {normalizar_url(u): t for u, t in paginas.items()}

    dados = registro.model_dump(mode="json")
    dados["paginas"] = paginas
    dados["demonstracao"] = True
    for passo in dados["passos"]:
        for f in passo["ferramentas"]:
            if f["nome"] == "buscar_na_web":
                f["resultado"] = _cortar_busca(f["resultado"])
            elif f["nome"] == "ler_pagina":
                f["resultado"] = _refazer_leitura(f["resultado"], por_url)
    recortado = RegistroPesquisa.model_validate(_ocultar(dados))  # validado de novo: o arquivo sai no formato certo

    try:
        refeito = repetir(recortado).registro.resultado
    except ErroDeReplay as erro:
        raise ErroDeSanitizacao(f"o registro recortado não pôde ser repetido: {erro}") from None
    antes, depois = _impressao_digital(registro.resultado), _impressao_digital(refeito)
    if antes != depois:
        raise ErroDeSanitizacao(
            "o recorte mudaria a conferência.\n"
            f"  válidos   antes: {antes[0]}\n  válidos   depois: {depois[0]}\n"
            f"  recusados antes: {antes[1]}\n  recusados depois: {depois[1]}"
        )
    return recortado


def main(argumentos: list[str] | None = None) -> int:
    leitor = argparse.ArgumentParser(description="Recorta uma execução gravada para o exemplo público.")
    leitor.add_argument("entrada", type=Path)
    leitor.add_argument("saida", type=Path)
    opcoes = leitor.parse_args(argumentos)
    original = carregar_registro(opcoes.entrada)
    try:
        recortado = sanitizar(original)
    except ErroDeSanitizacao as erro:
        print(f"Recorte recusado: {erro}", file=sys.stderr)
        return 1
    salvar_registro(recortado, opcoes.saida)
    validos, recusados = _impressao_digital(recortado.resultado)
    print(f"{opcoes.entrada}: {len(registro_em_json(original)) / 1024:.1f} KB -> "
          f"{opcoes.saida}: {len(registro_em_json(recortado)) / 1024:.1f} KB")
    print(f"válidos (iguais antes e depois): {[mpn for mpn, _ in validos]}")
    print(f"recusados (iguais antes e depois): {recusados}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
