"""Testes automáticos do projeto (LLMs e buscadores simulados: sem rede, sem créditos)."""

# Chaves FALSAS: os clientes exigem as variáveis de ambiente, mas nenhum teste fala com a rede.
# `setdefault` não sobrescreve nada; o `.env` real nunca é lido nos testes (`load_dotenv` é desligado com `patch`).
import os

# Os clientes chamam `load_dotenv()` ao serem IMPORTADOS, o que leria o `.env` de quem roda os testes. Trocamos a função
# por uma que não faz nada ANTES de qualquer import do backend: assim nenhum teste lê o `.env`, nem por acidente.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False

os.environ.setdefault("TOGETHER_API_KEY", "chave-falsa-de-teste")
os.environ.setdefault("TAVILY_API_KEY", "chave-falsa-de-teste")

# Nenhum teste pode tocar a rede ao baixar um arquivo. Sem DNS, `baixar_imagem` recusa tudo com "falha de DNS".
# Os testes do baixador passam o seu próprio `resolver` e um servidor falso.
import socket  # noqa: E402

from backend.compartilhado import download_seguro  # noqa: E402


def _sem_dns(host, porta):
    raise socket.gaierror("DNS bloqueado nos testes")


download_seguro._resolver_dns = _sem_dns
