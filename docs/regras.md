# Regras de conferência

"A LLM propõe, o código prova." A LLM registra cada componente com uma evidência por requisito; o código
(`backend/pesquisa/conferencia.py` e `backend/compartilhado/unidades.py`) confere cada uma contra as páginas que as
ferramentas leram de fato nesta pesquisa. Nada aqui usa LLM. As regras abaixo estão na ordem em que o código as aplica.

## Antes da conferência: o formato

O registro da LLM passa primeiro pelo Pydantic (`backend/compartilhado/modelos.py`). Se não passar, volta à LLM com a
lista de erros para ela corrigir. Entre as regras de formato:

- Toda evidência tem `requisito`, `situacao` (`confirmado`, `indicio`, `nao_encontrado` ou `contradiz`) e `citacao`.
  A citação só pode ficar vazia com `nao_encontrado`, e tem no máximo 400 caracteres.
- A `url` do achado precisa ser `http` ou `https`, com um nome de site.
- O `ciclo_de_vida` é `ativo`, `nrnd`, `obsoleto` ou `desconhecido`.
- Campos a mais são recusados.

## 1. A fonte: URL de uma página lida

A URL do achado precisa ser de uma página que as ferramentas **leram com sucesso nesta pesquisa**. As URLs são
comparadas normalizadas (esquema e nome do site em minúsculas, sem o `#fragmento` e sem a `/` final). Uma leitura que
falhou ("Failed to fetch url") ou veio vazia não conta.

Sem a página, o achado é **recusado** ("URL não está entre as páginas lidas"), e nenhuma evidência fica `confirmado`.

## 2. O código da peça (MPN), em três níveis

1. O MPN aparece na página (comparação normalizada; veja "Normalização" abaixo): ok.
2. Não aparece, mas a **família** aparece: o achado continua válido, com o alerta "MPN exato não encontrado na
   página; só a família (...)".
3. Nem o MPN nem a família aparecem: o achado é **recusado** ("MPN não aparece na página").

A família (`raiz_do_mpn`) é calculada assim, sobre o MPN normalizado:

| Regra | Exemplo |
|---|---|
| Com hífen: o que vem antes do primeiro hífen | `AMS1117-3.3` → `ams1117` |
| Sem hífen: letras + dígitos + até 2 letras de série | `NCP1117LPST33T3G` → `ncp1117lp` |
| Se a regra anterior pegou o MPN inteiro: só letras + dígitos | `LM317T` → `lm317` |
| Sem família: menos de 4 caracteres, igual ao próprio MPN ou começando por dígito | `AP-12`, `NCP1117`, `1N4007` |

Na página, a família não pode vir colada a uma letra ou dígito **antes**, nem a um dígito **depois**: `lm317` casa com
"LM317T" e "LM317 datasheet", mas não com "LM3175" (outra peça) nem com "XLM317".

## 3. Ciclo de vida

- `obsoleto` ou `nrnd` (não recomendado para projetos novos): o achado é **recusado**.
- `desconhecido`: alerta "ciclo de vida desconhecido"; o achado continua válido (datasheets raramente dizem "ativo").
- O ciclo de vida é um campo do achado, nunca uma evidência: uma evidência chamada "ciclo de vida" (ou
  "ciclo_de_vida", "lifecycle", "life cycle") é descartada sem alerta.

## 4. Cada evidência

### 4.1 A citação existe na página?

1. A citação é dividida nos `...` (e `…`): a LLM costuma juntar trechos separados da página numa citação só.
2. Fragmentos com menos de 12 caracteres (já normalizados) são descartados: "3.3 V" sozinho apareceria por acaso.
   Se não sobrar nenhum, a citação é "curta demais".
3. Cada fragmento restante precisa aparecer na página: primeiro na forma normalizada; se não, na **forma de reserva**.
4. A citação só vale se **todos** os fragmentos aparecem.

Se a citação não vale, uma evidência `confirmado` cai para `indicio` (com o alerta "citação não encontrada na
página" ou "citação curta demais"). Nas outras situações, só o alerta.

**Normalização**: decomposição Unicode (NFKD) com remoção dos acentos, minúsculas (`casefold`), "µ" (micro) e "μ" (mu)
unificados, todos os tipos de hífen e travessão viram "-", e qualquer sequência de espaços (inclusive o não
separável) vira um espaço.

**Forma de reserva**: só letras, dígitos e o separador decimal. Um `.` ou `,` entre dois dígitos vira `.`, e todo o
resto (espaços, hífens, pontuação) sai. Ela resolve a hifenização de PDFs ("Cur-\nrent" → `current`) e espaçamentos
estranhos ("up to1A"), sem juntar números diferentes: "3,3 V" e "3.3V" viram `3.3v`, mas "33 V" vira `33v`.

**Bordas numéricas** (nas duas formas): um fragmento que **começa** com dígito não pode casar logo depois de um
dígito, nem de um `.`/`,` colado a um dígito; um que **termina** com dígito não pode casar logo antes de um dígito, nem
de um `.`/`,` seguido de dígito. Assim, "3.3 V output current" não casa dentro de "13.3 V output current", e "1.5 A"
não casa dentro de "11.5 A" nem de "1.55 A". Se o fragmento aparece várias vezes, basta uma ocorrência com bordas
válidas.

### 4.2 A evidência é de um requisito do pedido?

O nome do requisito é comparado normalizado com os parâmetros do pedido. Se não for um deles:

- **Encapsulamento**: se o pedido tem `encapsulamento` e a evidência se chama "encapsulamento" (ou "package",
  "pacote"), ela é aceita quando a citação, na forma de reserva, **contém** o encapsulamento pedido ("SOT-223" casa
  com "SOT223" e com "SOT-223-4"). Senão, cai para `indicio` com alerta.
- Qualquer outro nome: cai para `indicio` com o alerta "requisito não existe no pedido".

### 4.3 Os números batem? (só para `confirmado`, com a citação achada)

1. A unidade do requisito vira a unidade-base ("mA" → "A"). Unidade não reconhecida: `indicio`.
2. Do texto da citação saem todas as quantidades (veja abaixo), e ficam só as da mesma grandeza do requisito.
3. Nenhuma quantidade nessa grandeza: `indicio` ("citação sem quantidade em V").
4. Pelo menos uma atende ao requisito: continua `confirmado`. Nenhuma atende: vira `contradiz`.

As comparações usam aritmética decimal exata (`Decimal`): "100 mA" é exatamente "0,1 A". Os operadores são `>=`,
`<=`, `=` (igualdade exata) e `faixa` (intervalo fechado: os dois limites entram).

## Unidades e quantidades

- **Unidades**: V, A, W, Ω (também "ohm", "ohms" e o sinal de ohm U+2126), Hz, °C (também com "º"), F e H.
- **Prefixos SI**: p, n, µ (também "μ" e "u"), m, k, M e G. Maiúsculas importam ("m" é mili, "M" é mega; "K" não é
  aceito). °C não aceita prefixo.
- **Número**: ponto ou vírgula decimal, com no máximo um separador; sinal opcional. Não há separador de milhar nem
  notação científica.
- **Dentro de um texto** (`quantidades_do_texto`): número, depois nada, um espaço ou **um hífen**, e a unidade ("3.3V",
  "3,3 V", "1-A", "3.3-V"). O número não pode vir colado a letra, dígito, ponto ou vírgula antes (códigos de peça como
  "TPS54331" não viram quantidade), e a unidade não pode continuar em letra ("5 Volts" e "10 Amps" não são lidos). O
  sinal só vale no começo de uma palavra: em "4.5-28 V" o hífen é de intervalo.

## Depois da conferência

- **Válido** é o achado que não foi recusado pelas regras 1, 2 ou 3. Evidências fracas (`indicio`, `contradiz`) não
  recusam o achado: ficam visíveis para a revisão humana.
- O **resumo conferido** é escrito pelo código: quantos componentes a LLM propôs, quais passaram e quais foram recusados,
  com o motivo. O texto livre da LLM é guardado como "Comentário da LLM (não conferido)", e o código avisa quando esse
  texto cita um componente recusado.

## Limitações conhecidas

1. **Uma citação genérica de família pode confirmar um MPN específico.** Se a página diz "Output voltage: 3.3 V" sobre
   a família, a evidência é confirmada para a variante que a LLM escolheu, e o código não distingue.
2. **A conferência prova a frase e o número, não a semântica.** Ela confere que a citação existe e que uma quantidade
   dela atende ao requisito. Não confere se a frase fala do parâmetro certo: "current limit: 1.1 A" atende a
   "corrente de saída ≥ 1 A", embora o limite de corrente não seja necessariamente a corrente de saída especificada.
3. **O MPN exato quase nunca aparece no texto extraído.** A leitura devolve trechos de PDFs, e o código completo (com
   sufixo de tensão e embalagem) costuma estar numa tabela que não veio. Por isso o alerta "só a família" é comum.
4. **Intervalos produzem uma só quantidade.** Em "4.5-28 V", só "28 V" é lido (o "4.5" não tem unidade colada). Um
   requisito sobre o mínimo da faixa não é confirmado por esse texto.
5. **As fontes às vezes são páginas de distribuidores**, não do fabricante. O prompt pede a página oficial, mas o
   código só confere que a página foi lida (aconteceu na execução de referência).
6. **O resultado varia entre execuções** com o mesmo pedido: a LLM escolhe buscas, páginas e componentes diferentes.
7. **O limite de pesquisas por sessão é um freio, não uma cota**: recarregar a página zera a contagem.
