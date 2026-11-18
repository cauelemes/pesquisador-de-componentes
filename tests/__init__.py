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
