# A Fase 3 explicada de forma muito simples

Imagens `docs/img-13` a `img-17`. Para o detalhe técnico, ver `docs/guia-fase3.md`.

## A comparação

| No dia a dia | No projeto | Ficheiro |
|---|---|---|
| o sensor que diz um número | o gateway OPC UA | (Fase 2) |
| o **carteiro** que ouve e escreve | o **collector** | `collector.py` |
| o **caderno** escrito à caneta | o **spool** (ficheiro SQLite no disco) | `spool.py` |
| a **impressão digital** de cada leitura | o **`event_id`** | `envelope.py` |
| uma **página** do caderno | um **lote** | `spool.py` |
| o **arquivista** | o **writer** | `writer.py` |
| a **gaveta de rascunho** | um **branch** do lakeFS | `lake.py` |
| o **porteiro** | o **gate G1** | `g1.py` |
| o **arquivo oficial** | o **`main`** do lakeFS | — |

## A história (imagem 13)

1. O sensor diz "5,07 bar".
2. O carteiro ouve e escreve no caderno.
3. O caderno guarda tudo, à caneta.
4. De minuto a minuto, o arquivista arranca uma página.
5. Faz uma cópia numa gaveta de rascunho.
6. O porteiro confere a página.
7. Se está bem, a página vai para o arquivo oficial.

Tudo isto existe para cumprir **três promessas**: não perder, não duplicar, e não deixar
entrar lixo no arquivo oficial.

## As três promessas, e onde estão no código

**Não perder (imagem 14).** O carteiro só segue depois de a tinta secar: a leitura só conta
quando está no disco.
→ Ver `spool.py`, linha 27: `PRAGMA synchronous=FULL`. É **uma** linha, mas é ela que obriga
o disco a gravar a sério a cada vez.

**Não duplicar (imagem 15).** Cada leitura tem uma impressão digital feita do que ela *diz*.
Se chegar duas vezes, o caderno recusa a segunda.
→ Ver `envelope.py`, linha 24 (`make_event_id`): repara nos campos que entram e nos que
**não** entram. E `spool.py`: a palavra `UNIQUE` na tabela e o `INSERT OR IGNORE` no `add`.

**Não entrar lixo (imagem 17).** O porteiro tem 8 regras; uma linha estranha e a página fica
na gaveta, com um relatório.
→ Ver `g1.py`, linha 42 (`validate`): cada `found.append(Violation(...))` é uma regra a falhar.

## A ideia mais importante: e se o arquivista desmaiar? (imagem 16)

Se o writer morrer a meio, quando volta **faz tudo outra vez desde o início**. Não estraga
nada, porque cada passo feito duas vezes dá o mesmo que feito uma (chama-se
**idempotência**):

- abrir uma gaveta já aberta → continua aberta
- pôr uma cópia que já lá está → não fica duplicada
- assinar o que já está assinado → "nada de novo"
- arquivar o que já está arquivado → "nada a juntar"

→ Ver `writer.py`, linha 23 (`process`). Lê-o de cima a baixo: são os passos da imagem 16,
pela mesma ordem. Depois vê `lake.py`, linhas 29 e 39: é aí que "nada de novo" e "nada a
juntar" são tratados como **sucesso** e não como erro.

## O que analisar, por ordem (5 sítios, ~15 minutos)

| # | Onde | O que procurar | Pergunta para ti |
|---|---|---|---|
| 1 | `envelope.py` linha 24 | que campos entram na impressão digital | porque é que a hora de chegada não entra? |
| 2 | `spool.py` linhas 27 e 42 | `synchronous=FULL` e `INSERT OR IGNORE` | o que acontecia sem cada uma? |
| 3 | `writer.py` linha 23 | os passos, por ordem, e os `self.hook(...)` | onde é que o writer pode morrer? |
| 4 | `lake.py` linhas 29 e 39 | o `return False` em "no changes" | porque é que um erro passa a sucesso? |
| 5 | `g1.py` linha 42 | as 8 regras | porque é que um relógio 10 min adiantado passa? |

## Os pontos fracos que vale a pena discutir

1. **Carteiro desligado = leituras perdidas.** Enquanto o collector não está a correr, o que o
   sensor diz não fica escrito em lado nenhum.
2. **Uma linha má prende a página inteira.** As linhas boas ficam na gaveta com a má.
3. **O caderno nunca deita páginas fora.** Cresce para sempre.
