# Segurança

## Chaves de API

- **Nunca commite chaves.** As chaves ficam no `.env` (modelo em `.env.example`) e a senha de acesso da interface em
  `.streamlit/secrets.toml` (modelo em `.streamlit/secrets.toml.example`). Os dois arquivos estão no `.gitignore`.
- **Achou uma chave exposta** (num commit, num log, numa captura de tela)? **Revogue-a no painel do provedor**
  (Together AI ou Tavily) e gere outra. Apagar o commit não basta: o histórico do git e cópias do repositório podem
  guardá-la.

## Como reportar um problema

Abra uma *issue* neste repositório descrevendo o problema, **sem** incluir chaves, senhas ou dados pessoais. Se o
problema permitir abusar do sistema, descreva só o necessário para reproduzi-lo e peça um canal privado na issue.

## O que o projeto faz para se proteger

As decisões de segurança (texto da web tratado como dado, validação Pydantic, planilha sem injeção de fórmula, nomes
fixos no pacote, servidor só em `localhost`, senha comparada em tempo constante) estão descritas na seção "Decisões
de design e segurança" do [README](README.md).
