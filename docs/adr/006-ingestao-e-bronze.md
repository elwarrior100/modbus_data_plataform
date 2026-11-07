# ADR-006 — Ingestão: spool no PostgreSQL, lotes congelados e escrita idempotente

**Data:** 2026-11-07 · **Estado:** aceite (revisto no mesmo dia: o spool passou de SQLite para PostgreSQL)

## Contexto
A Fase 3 leva os dados do gateway OPC UA ao bronze no lakeFS sem perder nem duplicar nada,
mesmo se o collector ou o writer morrerem a meio. O contrato está em
`docs/bronze-contract.md`.

## Decisões
| Decisão | Escolha | Alternativas | Razão |
|---|---|---|---|
| Buffer entre receber e escrever | **spool no PostgreSQL** (base `ingest`, no Postgres que já existe) | SQLite, Redpanda/Kafka, ficheiros soltos | Ordem de grandeza (~13 amostras por segundo) não justifica um broker (ADR-009 do plano). Postgres dá transações e `UNIQUE` como o SQLite, e mais: collector e writer ficam independentes e o spool vê-se de fora. Ver "Porque Postgres e não SQLite" |
| Como evitar duplicados na origem | `event_id` determinístico + índice único | dedup só na escrita | A repetição apanha-se no momento de entrar, e o id é recalculável pelo G1 |
| Como tornar a escrita repetível | **lotes congelados** com `batch_id` = hash das `event_id`; todos os passos idempotentes | transação distribuída | O lakeFS devolve erros de "já feito" (branch existe, commit sem alterações, nada a juntar) que se tratam como sucesso |
| Escrita no lakeFS | SDK Python `lakefs` + Parquet com `pyarrow` | boto3 pelo gateway S3, `lakectl` | Branches, commits com metadados e merge diretos; validado contra o cluster |
| Onde correm | **dois Deployments separados** (`collector` e `writer`) | um pod com dois contentores | Já não partilham um ficheiro: só falam com o Postgres |
| Partição | `dt`/`hour` de **`ingest_ts`** | `source_ts` | `source_ts` pode estar desalinhado ou ausente |

## Consequências
- Garantia: **pelo menos uma vez entre fases, mas com identidade estável**, o que na prática
  dá "exatamente uma vez" no bronze (o `event_id` é único).
- O G1 rejeita lotes inteiros (um `blocker` bloqueia o merge). Não há quarentena por linha.
- Falha depois do merge e antes de marcar `exported`: o lote continua `pending`, refaz-se, e todos os passos são "já feitos".
- O lote só tem três estados (`pending`, `exported`, `rejected`); os passos intermédios não se gravam (simplificação de 2026-11-07: eram informativos e cada um custava um fsync).
- O `main` do lakeFS recebe um merge por lote. Com lotes de ~60 s são ~1 440 por dia; a
  limpeza de branches antigos entra na Fase 4 (política de retenção).
- O caminho em tempo real para o Timescale (dashboards) fica como passo seguinte desta fase.

## Porque Postgres e não SQLite

A primeira versão usou SQLite e a comparação com o Postgres (que já estava no cluster) não
tinha sido feita. Feita depois, a escolha era apertada:

| | SQLite | PostgreSQL |
|---|---|---|
| Independência do collector | não depende da rede | **depende da rede e do Postgres** |
| Collector e writer | têm de partilhar um pod e um volume | **pods independentes** |
| Ver o spool de fora | só entrando no pod | **qualquer cliente SQL** |
| Peças novas | 0 | 0 (já existe) |

O custo real do Postgres é o primeiro ponto: se cair, o collector não consegue gravar, e o OPC UA
não guarda histórico. **Mitigação:** o collector guarda as amostras em memória e repete a cada
segundo até conseguir (`_Queue` em `collector.py`); só saem da fila depois de gravadas. Testado
reiniciando o Postgres com o collector a correr: queda de ~6 s, até 75 amostras retidas, fluxo
contínuo de 11 a 15 amostras por segundo, sem buracos nem duplicados.

**Limite conhecido:** se o próprio *collector* morrer com amostras ainda em memória, essas perdem-se
(o OPC UA não as reenvia).

## Migração (2026-11-07)

O spool SQLite antigo (26 117 amostras, 43 lotes) foi copiado para o Postgres com
`ingest/scripts/migrate_sqlite_to_postgres.py` antes de arrancar os pods novos. Copiar também as
amostras já exportadas era necessário: sem a memória delas, o collector novo gravava de novo os
valores iniciais que o gateway reenvia e o bronze ficava com duplicados. Tempo sem recolher:
35 segundos.
