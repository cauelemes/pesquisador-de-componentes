"""Utilidades para URLs usadas na pesquisa: padronizar um endereço para compará-lo com outro.

Servem ao agente e à conferência: comparar a URL que a LLM citou com as que as ferramentas leram de fato.
"""

from urllib.parse import urlsplit, urlunsplit


def normalizar_url(url: str) -> str | None:
    """Padroniza a URL para detectar repetidas (None se não for uma URL http/https utilizável).

    Mantemos o caminho e a query string (em muitos sites o produto está na query), mas
    ignoramos maiúsculas no endereço, o "#fragmento" e o "/" final.
    """
    partes = urlsplit(url.strip())
    if partes.scheme.lower() not in ("http", "https") or not partes.hostname:
        return None
    return urlunsplit((partes.scheme.lower(), partes.hostname.lower(), partes.path.rstrip("/"), partes.query, ""))
