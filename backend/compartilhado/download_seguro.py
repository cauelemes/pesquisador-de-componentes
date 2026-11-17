"""Baixa uma imagem da internet SEM deixar o servidor ser usado contra a própria rede (ataque "SSRF").

Por que existe: o endereço da imagem vem de uma página da web (ou de um agente que a leu). Se o servidor baixasse
qualquer endereço, um endereço malicioso poderia fazê-lo acessar coisas que só ele alcança: serviços internos, o
endereço de metadados da AWS (169.254.169.254, que entrega credenciais da instância), etc. As defesas, em ordem:

  1. O endereço precisa ser http/https, sem usuário e senha, só nas portas 80/443, com nome de site de verdade (não IP
     numérico, não nome sem ponto).
  2. O DNS é resolvido AQUI e TODOS os IPs devolvidos precisam ser públicos (nada de rede privada, loopback, link-local,
     CGNAT, multicast, IPv4 dentro de IPv6...).
  3. A conexão é feita DIRETO no IP já validado (o nome do site vai no cabeçalho `Host` e no SNI do TLS, que continua
     conferindo o certificado do nome). Assim, um DNS que muda de resposta entre a conferência e a conexão não ajuda.
  4. Redirecionamentos não são seguidos pelo cliente: cada salto (no máximo 3) passa de novo por 1 a 3.
  5. Limites: tempo por operação e no total, bytes lidos (já descomprimidos: protege de "bomba" gzip), tipo de conteúdo
     de imagem, sem cookies e sem proxy do ambiente.

O que isto NÃO faz: não confere se o arquivo é de fato uma imagem boa (quem usar o download precisa conferir o conteúdo).
TODO: o DNS do sistema operacional não tem limite de tempo aqui; se isso virar problema, resolver numa thread com prazo.
"""

import ipaddress
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx


class ErroDeDownload(Exception):
    """O download foi recusado ou falhou. A mensagem é escrita para o usuário."""


MAX_BYTES = 8 * 1024 * 1024  # o maior arquivo aceito, já descomprimido
TIMEOUT_SEGUNDOS = 10.0  # espera de cada operação de rede (conectar, enviar, receber um pedaço)
PRAZO_TOTAL_SEGUNDOS = 30.0  # prazo de todo o download, para um servidor lento não prender a página
MAX_REDIRECIONAMENTOS = 3
MAX_TENTATIVAS_DE_IP = 3  # se a conexão falhar, tenta os outros IPs do mesmo nome (até este número)
TAMANHO_MAXIMO_DA_URL = 2000
PORTAS_PERMITIDAS = (80, 443)
USER_AGENT = "Mozilla/5.0 (compatible; PesquisadorDeComponentes/1.0)"  # genérico; alguns sites recusam clientes sem nome

_REDIRECIONAMENTOS = {301, 302, 303, 307, 308}
# Alguns servidores entregam imagens como "binário genérico"; quem decide se é imagem é o próprio conteúdo, conferido por quem chama.
_TIPOS_GENERICOS = {"application/octet-stream", "binary/octet-stream"}
_PROIBIDO_NA_URL = re.compile(r"[\s\x00-\x1f\x7f\\]")
# Além do que `ip.is_global` já recusa (privado, loopback, link-local, reservado...), nunca vale um IPv4 escondido dentro
# de IPv6, os endereços de tradução (NAT64, 6to4) e o CGNAT.
_REDES_BLOQUEADAS = tuple(
    ipaddress.ip_network(r) for r in ("::ffff:0:0/96", "64:ff9b::/96", "2002::/16", "100.64.0.0/10")
)


@dataclass(frozen=True)
class ImagemBaixada:
    url_final: str  # o endereço de onde o arquivo veio (depois dos redirecionamentos)
    conteudo: bytes
    tipo_de_conteudo: str  # o que o servidor declarou (ex.: "image/png"); NÃO é prova do formato


def ip_publico(texto: str) -> bool:
    """Se `texto` é um IP de internet pública (e não de rede interna, loopback, metadados, multicast...)."""
    try:
        ip = ipaddress.ip_address(texto.split("%")[0])  # o "%" tira o escopo de IPv6 (ex.: fe80::1%eth0)
    except ValueError:
        return False
    if ip.is_multicast or not ip.is_global:
        return False
    return not any(ip in rede for rede in _REDES_BLOQUEADAS)  # versões diferentes nunca "estão dentro" uma da outra


def _resolver_dns(host: str, porta: int) -> list[str]:
    """Os IPs que o sistema operacional devolve para o nome (sem repetir, na ordem)."""
    infos = socket.getaddrinfo(host, porta, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _validar_endereco(url: str) -> tuple[str, str, int, str]:
    """Confere o TEXTO do endereço. Devolve (esquema, nome do site em ASCII, porta, caminho com parâmetros)."""
    if not isinstance(url, str) or not url.strip():
        raise ErroDeDownload("O endereço da imagem está vazio.")
    url = url.strip()
    if len(url) > TAMANHO_MAXIMO_DA_URL or _PROIBIDO_NA_URL.search(url):
        raise ErroDeDownload("O endereço da imagem é inválido (grande demais ou com caracteres proibidos).")
    try:
        partes = urlsplit(url)
        porta_declarada = partes.port
    except ValueError:
        raise ErroDeDownload("O endereço da imagem é inválido.") from None
    if partes.scheme not in ("http", "https"):
        raise ErroDeDownload("Só são aceitos endereços de imagem http ou https.")
    if "@" in partes.netloc:
        raise ErroDeDownload("Endereços com usuário e senha não são aceitos.")
    host = partes.hostname
    if not host:
        raise ErroDeDownload("O endereço da imagem não tem nome de site.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ErroDeDownload("Endereços com IP numérico não são aceitos: é preciso um nome de site.")
    if "." not in host or host.rsplit(".", 1)[-1].isdigit():
        raise ErroDeDownload("O endereço da imagem não tem um nome de site válido.")
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ErroDeDownload("O nome do site da imagem é inválido.") from None
    porta = porta_declarada or (443 if partes.scheme == "https" else 80)
    if porta not in PORTAS_PERMITIDAS:
        raise ErroDeDownload("Só são aceitos endereços nas portas 80 e 443.")
    caminho = partes.path or "/"
    if partes.query:
        caminho += "?" + partes.query
    return partes.scheme, host_ascii, porta, caminho


def _ips_publicos(host: str, porta: int, resolver: Callable[[str, int], list[str]]) -> list[str]:
    """Resolve o nome e só devolve se TODOS os IPs forem públicos (um IP interno no meio já recusa o endereço)."""
    try:
        ips = resolver(host, porta)
    except (OSError, UnicodeError):
        raise ErroDeDownload("Não foi possível encontrar o site da imagem (falha de DNS).") from None
    if not ips:
        raise ErroDeDownload("Não foi possível encontrar o site da imagem (falha de DNS).")
    if not all(ip_publico(ip) for ip in ips):
        raise ErroDeDownload("O endereço da imagem aponta para uma rede não pública; por segurança, não foi baixado.")
    return ips


def _tamanho_legivel(max_bytes: int) -> str:
    return f"{max_bytes // (1024 * 1024)} MB" if max_bytes >= 1024 * 1024 else f"{max_bytes} bytes"


def _ler_corpo(resposta: httpx.Response, url: str, max_bytes: int, relogio: Callable[[], float], inicio: float) -> ImagemBaixada:
    """Confere o cabeçalho da resposta e lê o corpo em pedaços, parando nos limites."""
    if resposta.status_code != 200:
        raise ErroDeDownload(f"O site da imagem respondeu com erro (HTTP {resposta.status_code}).")
    tipo = resposta.headers.get("content-type", "").split(";")[0].strip().lower()
    if tipo == "image/svg+xml":
        raise ErroDeDownload("Imagens SVG não são aceitas (use JPEG, PNG, WebP, GIF ou AVIF).")
    if not (tipo.startswith("image/") or tipo in _TIPOS_GENERICOS):
        raise ErroDeDownload("O endereço não devolveu uma imagem.")
    declarado = resposta.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > max_bytes:
        raise ErroDeDownload(f"A imagem é grande demais (o máximo é {_tamanho_legivel(max_bytes)}).")
    partes: list[bytes] = []
    total = 0
    try:
        for pedaco in resposta.iter_bytes():  # já descomprimido (gzip): o limite vale para o tamanho real
            total += len(pedaco)
            if total > max_bytes:
                raise ErroDeDownload(f"A imagem é grande demais (o máximo é {_tamanho_legivel(max_bytes)}).")
            if relogio() - inicio > PRAZO_TOTAL_SEGUNDOS:
                raise ErroDeDownload("O download da imagem demorou demais.")
            partes.append(pedaco)
    except httpx.HTTPError:
        raise ErroDeDownload("A imagem não pôde ser lida até o fim.") from None
    if not total:
        raise ErroDeDownload("O site devolveu uma resposta vazia.")
    return ImagemBaixada(url_final=url, conteudo=b"".join(partes), tipo_de_conteudo=tipo)


def baixar_imagem(
    url: str,
    *,
    resolver: Callable[[str, int], list[str]] | None = None,
    transporte: httpx.BaseTransport | None = None,
    max_bytes: int = MAX_BYTES,
    relogio: Callable[[], float] = time.monotonic,
) -> ImagemBaixada:
    """Baixa a imagem de `url` com as defesas descritas no topo do arquivo.

    `resolver`, `transporte` e `relogio` existem para os testes (sem rede, sem DNS e sem esperar de verdade).

    Raises:
        ErroDeDownload: o endereço não é aceito, a rede falhou ou a resposta passou de um limite. A mensagem é para o usuário.
    """
    resolver = resolver or _resolver_dns
    inicio = relogio()
    atual = url
    with httpx.Client(
        transport=transporte, timeout=httpx.Timeout(TIMEOUT_SEGUNDOS), follow_redirects=False, trust_env=False
    ) as cliente:
        for _ in range(MAX_REDIRECIONAMENTOS + 1):
            if relogio() - inicio > PRAZO_TOTAL_SEGUNDOS:
                raise ErroDeDownload("O download da imagem demorou demais.")
            esquema, host, porta, caminho = _validar_endereco(atual)
            ips = _ips_publicos(host, porta, resolver)
            porta_padrao = 443 if esquema == "https" else 80
            nome_no_cabecalho = host if porta == porta_padrao else f"{host}:{porta}"  # o que o navegador mandaria

            falha = "Não foi possível conectar ao site da imagem."
            resposta = None
            for ip in ips[:MAX_TENTATIVAS_DE_IP]:
                ip_na_url = f"[{ip}]" if ":" in ip else ip
                pedido = cliente.build_request(
                    "GET",
                    f"{esquema}://{ip_na_url}:{porta}{caminho}",
                    headers={"Host": nome_no_cabecalho, "User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5"},
                    extensions={"sni_hostname": host} if esquema == "https" else None,
                )
                try:
                    resposta = cliente.send(pedido, stream=True)
                    break
                except httpx.TimeoutException:
                    falha = "O site da imagem não respondeu a tempo."
                except httpx.HTTPError:
                    falha = "Não foi possível conectar ao site da imagem."
            if resposta is None:
                raise ErroDeDownload(falha)

            try:
                if resposta.status_code in _REDIRECIONAMENTOS:
                    destino = resposta.headers.get("location")
                    if not destino:
                        raise ErroDeDownload("O site da imagem mandou um redirecionamento sem destino.")
                    atual = urljoin(atual, destino)  # cada salto passa de novo pela validação, no começo do laço
                    continue
                return _ler_corpo(resposta, atual, max_bytes, relogio, inicio)
            finally:
                resposta.close()
    raise ErroDeDownload("O endereço da imagem tem redirecionamentos demais.")
