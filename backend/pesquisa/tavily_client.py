"""Cliente mínimo para buscar na web usando o Tavily.

Este módulo é a "porta de entrada" única para a busca: os agentes de pesquisa vão
chamar `buscar` em vez de falar com o SDK do Tavily diretamente. Assim, trocar de
serviço de busca no futuro mexe em um só lugar (mesma ideia do together_client.py).

Documentação da API: https://docs.tavily.com/documentation/api-reference/endpoint/search
"""

import math
import os
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import ValidationError
from tavily import TavilyClient
from tavily import errors as erros_tavily

from ..compartilhado import uso as contador_de_uso
from ..compartilhado.modelos import PaginaLida, ResultadoBusca

# Lê o arquivo .env da raiz do projeto (se existir). Variáveis já definidas no shell
# têm prioridade, como no together_client.py.
load_dotenv()

# Tempo máximo (em segundos) de espera por uma resposta antes de desistir.
TIMEOUT_SEGUNDOS = 30

# Limites que o Tavily aceita (consultas muito longas e mais de 20 resultados são recusados).
TAMANHO_MAXIMO_CONSULTA = 400
MAX_RESULTADOS_LIMITE = 20

# Filtros de domínio: poucos por busca e só nomes de site bem formados ("exemplo.com.br").
# A API do Tavily aceita até 300 sites para restringir (include) e 150 para excluir; ficamos bem abaixo dos dois. É só
# uma proteção contra excesso: o limite não muda custo nem qualidade da busca.
MAX_DOMINIOS_INCLUIR = 50  # restringir a busca a estes sites ("dominios")
MAX_DOMINIOS_EXCLUIR = 50  # sites a ignorar ("excluir_dominios"); o agente descarta o que passar disso e registra
_DOMINIO = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")

# Leitura de páginas: o Tavily cobra 1 crédito a cada 5 páginas lidas (modo básico).
MAX_URLS_LEITURA = 5
TIMEOUT_LEITURA_SEGUNDOS = 30

# A regra documentada do Tavily (modo básico), usada só como ESTIMATIVA quando a resposta não informa os créditos gastos:
# uma busca custa 1 crédito; a leitura custa 1 crédito a cada 5 páginas lidas com sucesso.
CREDITOS_DA_BUSCA_BASICA = 1
PAGINAS_POR_CREDITO_NA_LEITURA = 5


class ErroTavily(Exception):
    """Erro ao falar com o Tavily, com mensagem pensada para quem usa o sistema."""


def _registrar_creditos(resposta, *, operacao: str, estimativa: float) -> None:
    """Soma ao contador da sessão (`uso.py`) os créditos gastos. Pedimos `include_usage` e usamos o valor que o Tavily
    informa (`usage.credits`); se a resposta não trouxer, usa `estimativa` e marca como ESTIMADO. Sem contador ativo,
    não faz nada."""
    informado = resposta.get("usage") if isinstance(resposta, dict) else None
    creditos = informado.get("credits") if isinstance(informado, dict) else None
    if isinstance(creditos, (int, float)) and not isinstance(creditos, bool) and creditos >= 0:
        contador_de_uso.registrar_creditos_tavily(creditos, operacao=operacao)
    else:
        contador_de_uso.registrar_creditos_tavily(estimativa, operacao=operacao, estimado=True)


def _criar_cliente() -> TavilyClient:
    """Cria o cliente do Tavily a partir da chave guardada no ambiente."""
    chave = os.getenv("TAVILY_API_KEY")
    if not chave:
        # Falhamos cedo e sem expor nada sensível na mensagem.
        raise ErroTavily(
            "A variável TAVILY_API_KEY não está definida. "
            "Copie .env.example para .env e preencha a sua chave."
        )
    return TavilyClient(api_key=chave)


def _traduzir_erro(erro: Exception) -> ErroTavily:
    """Converte erros do SDK em mensagens claras, sem vazar a chave de API."""
    if isinstance(erro, (erros_tavily.InvalidAPIKeyError, erros_tavily.MissingAPIKeyError)):
        return ErroTavily("Chave de API recusada pelo Tavily. Confira o valor de TAVILY_API_KEY no .env.")
    if isinstance(erro, erros_tavily.UsageLimitExceededError):
        return ErroTavily("Limite de uso do plano do Tavily atingido. Verifique os créditos da conta.")
    if isinstance(erro, erros_tavily.ForbiddenError):
        return ErroTavily("O Tavily negou o acesso a este recurso (403).")
    if isinstance(erro, erros_tavily.BadRequestError):
        return ErroTavily("O Tavily recusou a requisição (400): confira a consulta e os parâmetros.")
    if isinstance(erro, erros_tavily.TimeoutError):
        return ErroTavily(f"O Tavily não respondeu em {TIMEOUT_SEGUNDOS} s. Tente novamente.")
    # Qualquer outro erro (rede, resposta inesperada...): só o tipo, nunca o texto, que
    # poderia conter detalhes da requisição.
    return ErroTavily(f"Não foi possível falar com o Tavily ({type(erro).__name__}).")


def _validar_dominios(dominios: list[str] | None, nome: str, maximo: int = MAX_DOMINIOS_INCLUIR) -> list[str]:
    """Confere e padroniza uma lista de domínios ("www.Exemplo.com.br" -> "exemplo.com.br")."""
    if not dominios:
        return []
    if len(dominios) > maximo:
        raise ErroTavily(f"{nome}: no máximo {maximo} domínios por busca.")
    limpos = []
    for dominio in dominios:
        limpo = str(dominio).strip().casefold().removeprefix("www.")
        if not _DOMINIO.fullmatch(limpo):
            raise ErroTavily(f"{nome}: '{dominio}' não parece um domínio válido (ex.: exemplo.com.br).")
        limpos.append(limpo)
    return limpos


def buscar(
    consulta: str,
    *,
    max_resultados: int = 5,
    pais: str | None = None,
    idioma: str | None = None,
    dominios: list[str] | None = None,
    excluir_dominios: list[str] | None = None,
) -> list[ResultadoBusca]:
    """Busca `consulta` na web e devolve os resultados, do mais ao menos relevante.

    Args:
        consulta: o texto da busca (como se fosse digitado num buscador).
        max_resultados: quantos resultados pedir (1 a 20). Cada busca gasta créditos do plano.
        pais: nome do país em inglês e minúsculas (ex.: "brazil"). Apenas FAVORECE
            resultados daquele país no ranking; não exclui os demais.
        idioma: código ISO 639-1 (ex.: "pt"). Também apenas favorece o idioma; escreva a
            consulta no mesmo idioma para melhores resultados.
        dominios: se informado, a busca fica RESTRITA a estes sites (ex.: ["exemplo.com.br"]),
            útil para procurar um produto dentro de um site específico.
        excluir_dominios: sites a ignorar nos resultados (ex.: marketplaces).

    Returns:
        Lista de `ResultadoBusca` (pode ser vazia se nada foi encontrado).

    Raises:
        ErroTavily: consulta inválida, chave ausente/recusada, limite de uso, falha de rede etc.
    """
    consulta = consulta.strip()
    if not consulta:
        raise ErroTavily("A consulta de busca está vazia.")
    if len(consulta) > TAMANHO_MAXIMO_CONSULTA:
        raise ErroTavily(f"Consulta longa demais (máximo {TAMANHO_MAXIMO_CONSULTA} caracteres).")
    if not 1 <= max_resultados <= MAX_RESULTADOS_LIMITE:
        raise ErroTavily(f"max_resultados deve estar entre 1 e {MAX_RESULTADOS_LIMITE}.")

    # Parâmetros opcionais: só entram na chamada se foram informados (o SDK ignora None,
    # mas assim fica explícito o que está sendo enviado).
    opcionais = {}
    if pais:
        opcionais["country"] = pais
    if idioma:
        opcionais["language"] = idioma
    incluir = _validar_dominios(dominios, "dominios")
    excluir = _validar_dominios(excluir_dominios, "excluir_dominios", MAX_DOMINIOS_EXCLUIR)
    if incluir:
        opcionais["include_domains"] = incluir  # por padrão o Tavily RESTRINGE a estes sites
    if excluir:
        opcionais["exclude_domains"] = excluir

    cliente = _criar_cliente()
    try:
        # "basic" é a busca mais barata (1 crédito). Sem filtros de domínio por ora, pois
        # queremos DESCOBRIR sites novos, e não restringir a sites que já conhecemos.
        # TODO: calibrar search_depth ("advanced" custa 2 créditos), exclude_domains
        #       (marketplaces) e pais/idioma com casos reais.
        resposta = cliente.search(
            consulta,
            search_depth="basic",
            max_results=max_resultados,
            timeout=TIMEOUT_SEGUNDOS,
            include_usage=True,  # a resposta informa os créditos gastos (ver `_registrar_creditos`)
            **opcionais,
        )
    except Exception as erro:  # o SDK e o `requests` levantam vários tipos; traduzimos todos
        raise _traduzir_erro(erro) from None
    _registrar_creditos(resposta, operacao="busca", estimativa=CREDITOS_DA_BUSCA_BASICA)

    resultados = []
    for item in resposta.get("results", []):
        try:
            resultados.append(
                ResultadoBusca(
                    titulo=item.get("title") or "",
                    url=item.get("url") or "",
                    conteudo=item.get("content") or "",
                    pontuacao=item.get("score"),
                )
            )
        except ValidationError:
            # Resultado sem URL ou com campos inesperados: descartamos só ele.
            continue
    return resultados


def _url_valida(url: str) -> bool:
    """Só aceitamos endereços http/https com um site (nada de file:, javascript: etc.)."""
    partes = urlsplit(url.strip())
    return partes.scheme.lower() in ("http", "https") and bool(partes.hostname)


# Heurística para separar a foto do produto do "enfeite" da página. TODO: calibrar com páginas reais
# (hoje só descarta o óbvio; a escolha final entre as candidatas é do agente e depois do usuário).
EXTENSOES_DE_IMAGEM = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")
PALAVRAS_DE_ENFEITE = (
    "logo", "icon", "favicon", "sprite", "banner", "placeholder", "loading", "pixel", "avatar",
    "whatsapp", "facebook", "instagram", "youtube", "selo", "seal", "payment", "pagamento",
)
MAX_IMAGENS_POR_PAGINA = 5


def _filtrar_imagens(imagens: list) -> list[str]:
    """Fica com links http/https de imagens que parecem fotos (sem logos/ícones), sem repetir.

    Devolve no máximo `MAX_IMAGENS_POR_PAGINA`, na ordem em que a página as traz.
    """
    escolhidas: list[str] = []
    for imagem in imagens:
        if not isinstance(imagem, str) or not _url_valida(imagem) or imagem in escolhidas:
            continue
        caminho = urlsplit(imagem.strip()).path.casefold()
        if not caminho.endswith(EXTENSOES_DE_IMAGEM):
            continue
        if any(palavra in imagem.casefold() for palavra in PALAVRAS_DE_ENFEITE):
            continue
        escolhidas.append(imagem.strip())
        if len(escolhidas) == MAX_IMAGENS_POR_PAGINA:
            break
    return escolhidas


def extrair(urls: list[str], *, foco: str, trechos_por_pagina: int = 3) -> list[PaginaLida]:
    """Lê páginas e devolve os trechos mais relevantes para o `foco` (não a página inteira).

    Serve para CONFERIR uma página de produto (ex.: se ela traz o diâmetro pedido) sem
    levar o texto todo para dentro do contexto de uma LLM.

    Args:
        urls: de 1 a 5 páginas. O Tavily cobra 1 crédito a cada 5 páginas lidas com sucesso.
        foco: o que procurar na página (ex.: "diâmetro 38 mm, comprimentos, fixação").
        trechos_por_pagina: até quantos trechos (~800 caracteres cada) devolver por página (1 a 5).

    Returns:
        Uma `PaginaLida` por URL pedida. Páginas que falharam voltam com `erro` preenchido
        (uma página ruim não derruba as demais).

    Raises:
        ErroTavily: argumentos inválidos, chave ausente/recusada, limite de uso, falha de rede etc.
    """
    foco = foco.strip()
    if not foco:
        raise ErroTavily("O foco da leitura está vazio.")
    if len(foco) > TAMANHO_MAXIMO_CONSULTA:
        raise ErroTavily(f"Foco longo demais (máximo {TAMANHO_MAXIMO_CONSULTA} caracteres).")
    if not 1 <= len(urls) <= MAX_URLS_LEITURA:
        raise ErroTavily(f"Informe de 1 a {MAX_URLS_LEITURA} URLs por leitura.")
    invalidas = [u for u in urls if not _url_valida(u)]
    if invalidas:
        raise ErroTavily(f"URL inválida (use http ou https): {invalidas[0][:100]}")
    if not 1 <= trechos_por_pagina <= 5:
        raise ErroTavily("trechos_por_pagina deve estar entre 1 e 5.")

    cliente = _criar_cliente()
    try:
        # "basic" é o modo mais barato. `query` faz o Tavily devolver só os trechos relevantes.
        resposta = cliente.extract(
            urls=[u.strip() for u in urls],
            query=foco,
            chunks_per_source=trechos_por_pagina,
            extract_depth="basic",
            include_images=True,  # os links das imagens da página (para a imagem do produto na ficha)
            include_usage=True,  # a resposta informa os créditos gastos (ver `_registrar_creditos`)
            timeout=TIMEOUT_LEITURA_SEGUNDOS,
        )
    except Exception as erro:
        raise _traduzir_erro(erro) from None
    lidas_com_sucesso = len(resposta.get("results", []))
    _registrar_creditos(
        resposta, operacao="leitura", estimativa=math.ceil(lidas_com_sucesso / PAGINAS_POR_CREDITO_NA_LEITURA)
    )

    # A resposta tem duas listas e a ordem não é garantida: lemos as duas e devolvemos na
    # ordem pedida. Uma URL que não aparece em nenhuma delas conta como falha.
    por_url: dict[str, PaginaLida] = {}
    for item in resposta.get("results", []):
        url = item.get("url") or ""
        if url:
            por_url[url.rstrip("/").casefold()] = PaginaLida(
                url=url,
                conteudo=item.get("raw_content") or "",
                imagens=_filtrar_imagens(item.get("images") or []),
            )
    for item in resposta.get("failed_results", []):
        url = item.get("url") or ""
        if url:
            motivo = str(item.get("error") or "falha ao ler a página")[:200]
            por_url.setdefault(url.rstrip("/").casefold(), PaginaLida(url=url, erro=motivo))

    paginas = []
    for url in urls:
        url = url.strip()
        paginas.append(por_url.get(url.rstrip("/").casefold()) or PaginaLida(url=url, erro="sem resposta do Tavily"))
    return paginas
