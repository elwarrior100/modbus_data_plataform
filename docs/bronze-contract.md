# Contrato do bronze e da ingestão (Fase 3)

Define o que o collector e o writer produzem, e as regras do gate G1. É o contrato com a
Fase 4 (silver): quem lê o bronze só precisa de saber isto.

## 1. O caminho dos dados

    gateway OPC UA ──► collector ──► spool (PostgreSQL) ──► writer ──► lakeFS (branch → main)
       (valores)      (subscrição)    (durável)        (em lotes)       bronze/

- O **collector** subscreve todas as variáveis e escreve cada alteração no **spool**.
- O **spool** são duas tabelas (`events` e `batches`) na base `ingest` do PostgreSQL. Uma amostra só
  sai da memória do collector **depois de gravada** (se o Postgres falhar, fica retida e repete-se).
- O **writer** agrupa amostras em **lotes**, escreve um Parquet por partição num **branch**
  do lakeFS, valida (G1) e só então junta ao `main`.

## 2. O envelope (uma linha por amostra recebida)

Sem interpretação semântica: nada é convertido, escalado nem corrigido. Valores `Bad` também
são guardados: a ausência de dados é informação.

| Coluna | Tipo | Descrição |
|---|---|---|
| `event_id` | string | UUID determinístico (v5) de `source`, `opcua_node_id`, `source_ts`, `opcua_status_code` e `raw_value`. A mesma amostra dá sempre o mesmo id |
| `source` | string | quem produziu: `ot-gateway` |
| `opcua_node_id` | string | ex.: `ns=2;s=PUMP-01.outlet_pressure` |
| `raw_value` | string, nulo | o valor em JSON (`"5.07"`, `"true"`); nulo quando o estado é `Bad` |
| `opcua_status_code` | uint32 | o StatusCode OPC UA, sem tradução (`0` = Good) |
| `source_ts` | timestamp UTC, nulo | `SourceTimestamp` (relógio do dispositivo) |
| `server_ts` | timestamp UTC, nulo | `ServerTimestamp` (relógio do gateway) |
| `ingest_ts` | timestamp UTC | quando o collector recebeu a amostra |
| `schema_version` | string | `1` |
| `batch_id` | string | o lote em que a amostra foi escrita |

`server_ts` não estava no plano original: acrescenta-se porque é o que permite detetar um
`clock_skew` mais tarde (comparar o relógio do dispositivo com o do gateway).

## 3. Onde fica no lakeFS

Repositório `industrial-lake`, caminho:

    bronze/dt=YYYY-MM-DD/hour=HH/<batch_id>.parquet

- `dt` e `hour` vêm de **`ingest_ts`** (existe sempre e é o relógio do collector; o
  `source_ts` pode estar desalinhado ou ausente). Só existem no caminho, não dentro do ficheiro.
- Um lote que atravessa uma hora escreve **um ficheiro por partição**, com o mesmo `batch_id`.
- **Nunca se reescreve nem se corrige o bronze.** Um erro corrige-se no silver.
- **Ficheiros do spike:** o `main` ainda tem `bronze/dt=2026-03-0X/data_0.parquet` (dados sintéticos
  da Fase 0, outro esquema, sem `hour=`), de que dependem os testes de fumo da Fase 0. O bronze
  real distingue-se por ter sempre `hour=HH`. Quem lê o bronze (Fase 4) usa
  `bronze/dt=*/hour=*/*.parquet`. Limpar estes ficheiros fica anotado para quando o teste
  de fumo da Fase 0 for reformulado.

## 4. Lotes e o ciclo de escrita

Um lote fica **congelado** no spool no momento em que é formado (um `batch_id` fixo por
conjunto de amostras). É isto que torna a escrita repetível: reiniciar nunca reagrupa
amostras. O `batch_id` é o hash das `event_id` do lote.

Um lote tem só **três estados** no spool:

| Estado | Significa |
|---|---|
| `pending` | congelado, ainda por escrever (ou a meio de ser escrito) |
| `exported` | já está no `main` do lakeFS; estado final |
| `rejected` | o G1 recusou-o; o branch ficou por juntar; estado final |

Para o levar de `pending` a `exported`, o writer executa estes passos, **todos idempotentes**
(repetir dá o mesmo):

| # | Passo | O que faz |
|---|---|---|
| 1 | branch | cria o branch `etl-<batch_id>` (se já existir, continua) |
| 2 | upload | carrega o Parquet de cada partição (caminho determinístico, por isso sobrescreve) |
| 3 | commit | commit no branch, com `batch_id`, nº de linhas e intervalo nos metadados |
| 4 | validar | lê os ficheiros de volta do branch e corre o G1 (se falhar: `rejected`, e pára aqui) |
| 5 | merge | junta o branch ao `main` |
| 6 | exportado | marca o lote como `exported` no spool |

Os passos intermédios **não ficam gravados**: se o writer morrer em qualquer ponto, ao
reiniciar vê o lote ainda `pending` e **repete tudo desde o passo 1**. "Branch já existe",
"commit sem alterações" e "nada a juntar" tratam-se como passos já feitos.

## 5. Gate G1: contrato

Corre **no branch**, antes do merge. Qualquer falha é `blocker`: o lote fica `rejected`, o
branch **fica por juntar** (para autópsia) com um `g1_report.json`, e o `main` não é tocado.

| Regra | Verifica |
|---|---|
| G1-01 | o Parquet tem exatamente as colunas e tipos do envelope |
| G1-02 | `event_id` únicos no lote e iguais ao hash recalculado |
| G1-03 | `opcua_node_id` está na lista de nós conhecidos (`ingest/config/nodes.yaml`) |
| G1-04 | `raw_value` é JSON válido e do tipo esperado para o nó |
| G1-05 | estado `Bad` ⇒ `raw_value` nulo; estado `Good` ou `Uncertain` ⇒ não nulo |
| G1-06 | `source_ts` entre há 365 dias e daqui a 1 dia (só rejeita lixo, não desalinhamentos) |
| G1-07 | `ingest_ts` não está mais de 5 minutos no futuro |
| G1-08 | o número de linhas lidas do lakeFS é igual ao do spool para o lote |

Um `clock_skew` de 10 minutos **passa** o G1 de propósito: detetá-lo é trabalho do G2.

## 6. O que o collector garante

- **Reconexão com espera crescente** (1 s, 2 s, 4 s… até 30 s) se o gateway cair.
- **Sem duplicados:** o spool tem índice único em `event_id`; uma amostra repetida (por exemplo
  o valor inicial que o OPC UA reenvia ao subscrever) é ignorada.
- **Nada se perde entre receber e escrever:** a amostra está no disco antes de ser confirmada.

## 7. Critérios de verificação

1. matar o writer em cada passo do ciclo e reiniciar: **zero duplicados, zero perdas**
   (contagem de `event_id` distintos no lakeFS igual à do spool);
2. injetar uma amostra malformada: o lote fica `rejected` e o `main` não muda;
3. desligar o gateway durante minutos: o collector reconecta sozinho;
4. correr horas seguidas e reconciliar o spool com o lakeFS sem diferenças.
