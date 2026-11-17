"""A versão do sistema, escrita num lugar só.

Quem precisa do número (hoje, o registro JSON de cada pesquisa) importa `VERSAO` daqui; nenhum outro arquivo repete o
número.

Padrão SemVer (https://semver.org/lang/pt-BR/): MAIOR.MENOR.CORREÇÃO, com o sufixo `-beta` enquanto o sistema está em
teste. Enquanto o MAIOR for 0, uma correção sobe o último número e uma funcionalidade nova sobe o do meio.

Regra de uso: mude o número no commit que marca uma versão e crie, nesse mesmo commit, a tag `v<VERSAO>`
(ex.: `git tag -a v0.1.0-beta -m "..."`).
"""

VERSAO = "0.1.0-beta"
