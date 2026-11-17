"""Utilidades para URLs usadas na pesquisa (padronizar e achar o "site" de um endereço).

Vieram do antigo agrupador de resultados e servem ao agente: comparar a URL que ele citou com
as que as ferramentas devolveram, e saber se duas URLs são do mesmo site.
"""

from urllib.parse import urlsplit, urlunsplit

# Segundos níveis comuns em domínios como "empresa.com.br". Serve para saber quantos pedaços
# do endereço formam o "domínio base". TODO: trocar por uma lista completa de sufixos
# públicos (biblioteca `tldextract` ou similar) se esta heurística falhar.
SEGUNDOS_NIVEIS = {"com", "ind", "net", "org", "gov", "edu", "eng"}


def normalizar_url(url: str) -> str | None:
    """Padroniza a URL para detectar repetidas (None se não for uma URL http/https utilizável).

    Mantemos o caminho e a query string (em muitos sites o produto está na query), mas
    ignoramos maiúsculas no endereço, o "#fragmento" e o "/" final.
    """
    partes = urlsplit(url.strip())
    if partes.scheme.lower() not in ("http", "https") or not partes.hostname:
        return None
    return urlunsplit((partes.scheme.lower(), partes.hostname.lower(), partes.path.rstrip("/"), partes.query, ""))


def dominio_base(url_normalizada: str) -> str:
    """Domínio base do site: 'blog.exemplo.com.br' e 'www.exemplo.com.br' viram 'exemplo.com.br'."""
    host = urlsplit(url_normalizada).hostname or ""
    partes = host.split(".")
    if len(partes) >= 3 and partes[-2] in SEGUNDOS_NIVEIS:
        return ".".join(partes[-3:])
    return ".".join(partes[-2:])
