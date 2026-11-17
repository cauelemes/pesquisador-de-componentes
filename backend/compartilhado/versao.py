"""A versão do sistema, escrita num lugar só.

Quem precisa do número (a linha do menu lateral, os arquivos de debug) importa `VERSAO` daqui; nenhum outro arquivo
repete o número.

Padrão SemVer (https://semver.org/lang/pt-BR/): MAIOR.MENOR.CORREÇÃO, com o sufixo `-beta` enquanto o sistema está em
teste com usuários. Enquanto o MAIOR for 0, a correção pequena sobe o último número e a funcionalidade nova sobe o
do meio.

Regra de uso: o número aqui é o do commit que recebe a tag `v<VERSAO>` (ex.: `git tag -a v0.2.0-beta -m "..."`) e vai para
o servidor. Mude-o no commit da versão, antes de criar a tag. A tag `v0.1.0-beta` marca a primeira implantação e é
anterior a este arquivo: o código dela não mostra versão.

Durante o desenvolvimento, o número é o da PRÓXIMA versão a lançar, com o sufixo `.dev` (ex.: `0.2.0-beta.dev`): quem vê
esse número no menu lateral ou num log sabe que o código ainda não foi lançado. Os commits do dia a dia não mexem aqui. No
commit do lançamento, tire git o `.dev` (`0.2.0-beta`), crie a tag `v0.2.0-beta` nesse commit e atualize o servidor. Uma
correção urgente antes disso leva o seu próprio número (ex.: `0.1.1-beta`) só no commit dela.
No MESMO commit do lançamento, acrescente também as novidades da versão no COMEÇO de `HISTORICO`, em
`frontend/historico_de_versoes.py`, com a `data` do deploy (o "o que há de novo" da tela de acesso; as versões antigas ficam lá, e
`MAX_VERSOES_EXIBIDAS`, em `frontend/novidades.py`, diz quantas aparecem); um teste falha se esquecer.
"""

VERSAO = "0.1.0-beta"
