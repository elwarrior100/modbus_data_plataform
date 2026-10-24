# ADR-000 — Spike: leitura de Parquet num branch lakeFS com DuckDB

**Data:** 2026-09-05 · **Estado:** concluído (2026-10-24)
**Timebox:** 2 h · **Contexto:** [PLAN.md §13](../PLAN.md)

## Pergunta

O desenho Write-Audit-Publish da [§2 do plano](../PLAN.md) assume que se escreve Parquet
num branch lakeFS, se valida lá, e só depois se promove por merge. Isso só é viável se o
motor de transformação conseguir ler dados **de uma ref específica**, com desempenho
aceitável. Se não conseguir, as Fases 4-8 mudam todas.

**É viável?**

## Montagem

| Peça | Versão / configuração |
|---|---|
| lakeFS | 1.86.0, Docker, modo `--quickstart` (blockstore local, dados dentro do container) |
| DuckDB | 1.5.5, em memória, extensão `httpfs` |
| Repositório | `spike`, storage namespace `local://spike` |
| Refs | `main` (vazia) e `etl-spike` |
| Dataset | 100 000 linhas · 3 ativos × 6 tags · 3 dias · particionado por `dt` · 3 ficheiros · 840 kB |
| Host | macOS, tudo em localhost |

Parâmetros do DuckDB para falar com o gateway S3 do lakeFS:

| Parâmetro | Valor | Nota |
|---|---|---|
| `s3_endpoint` | `127.0.0.1:8000` | sem esquema `http://` |
| `s3_url_style` | `path` | obrigatório: em virtual-host o cliente procuraria `spike.127.0.0.1:8000` |
| `s3_use_ssl` | `false` | lakeFS local em HTTP |
| `s3_region` | `us-east-1` | entra no cálculo da assinatura AWS |
| `s3_access_key_id` / `s3_secret_access_key` | credenciais do lakeFS | **ambas** obrigatórias |

Endereçamento: o **repositório é o bucket** e a **ref é o primeiro segmento do caminho** —
`s3://spike/etl-spike/bronze/…`. Trocar de ref é trocar uma palavra no caminho.

## Resultados

### Ponto 4 — isolamento do branch ✅

`lakectl fs ls` sobre `etl-spike/bronze/` lista as três partições; sobre `main/bronze/`
não lista nada. Dados escritos num branch são invisíveis em `main` até haver merge.

### Achado colateral — staging não é commit ⚠️

Depois do upload e **antes** do commit:

- `lakectl diff <ref> <ref>` entre `main` e `etl-spike` vem **vazio**, e ambos os branches
  apontam ao mesmo commit — `diff` compara commits, e não havia commit novo.
- `lakectl diff` com **uma ref só** mostra os três objetos como `+ added`: é a área de
  staging, equivalente ao `git status`.
- `lakectl log` do branch mostra só "Repository created".
- Um merge neste estado não teria transportado nada.

Consequência para o desenho: o passo de commit é obrigatório e explícito no DAG. Escrever
não promove; commitar não promove; só o merge promove.

### Ponto 5 — leitura e glob ✅

Leitura de um ficheiro explícito e glob recursivo (`bronze/**/*.parquet`) funcionam ambos:
100 000 linhas lidas através do gateway. A coluna de partição `dt` é devolvida.

Custou 30 minutos por um erro de configuração: `s3_secret_access_key` não definida →
`HTTP 403 SignatureDoesNotMatch`. O diagnóstico saiu do próprio erro: houve resposta HTTP
(logo endpoint e SSL certos), o URL construído tinha o bucket no caminho (logo `url_style`
certo), e a falha era de assinatura (logo credenciais).

### Ponto 6 — desempenho ✅

Segunda execução de cada consulta, com `.timer on`:

| Consulta | Local | Via lakeFS | Diferença |
|---|---|---|---|
| `COUNT(*)` — só metadados do Parquet | 0.005 s | 0.021 s | 16 ms |
| `SUM(value)` — lê a coluna | 0.004 s | 0.015 s | **11 ms** |

**Conclusão:** o gateway S3 do lakeFS acrescenta um custo **fixo de ~4 ms por ficheiro** e
**nenhum custo proporcional ao volume lido** — a diferença não cresceu ao passar de leitura
de metadados para leitura de dados. Os resultados são **bit-a-bit idênticos** aos da leitura
local (`SUM` = `5005544.534905481` nos dois casos), pelo que o gateway não altera dados.

**Ressalva:** medido com 3 ficheiros, 840 kB, em localhost, sem concorrência. Não diz nada
sobre o custo de **listagem** com milhares de objetos, nem sobre débito a dezenas de GB.
Repetir a uma escala realista antes de fechar a Fase 4.

### Repetição no cluster (k3s + SeaweedFS + lakeFS em Postgres)

O spike inicial usou lakeFS em Docker com blockstore local. Foi repetido contra o
cluster real da Fase 0, com o lakeFS 1.86.0 a escrever para SeaweedFS (S3), metadados em
PostgreSQL e DuckDB 1.5.5 no Mac a ler pelo gateway S3 do lakeFS via port-forward.

| Ponto | Resultado |
|---|---|
| Isolamento (branch invisível em `main` até ao merge) | ✅ repetido no cluster |
| Commit, merge e tag `gold-v0.1.0` | ✅ |
| Leitura por tag com glob | ✅ 100 000 linhas, soma `5005544.534905481` |
| Igualdade com a leitura local | ✅ soma idêntica, bit a bit |
| Compatibilidade lakeFS sobre SeaweedFS | ✅ escrita, commit, merge e leitura |

### Ponto 5b — origem da coluna `dt` ✅

Com `hive_partitioning` desligado, `dt` **desaparece** (restam `asset_id`, `tag_name`,
`ts_utc`, `value`, `row_index`). O valor vive só no nome da pasta: não há duplicação em
disco e é possível *partition pruning* por data.

### Ponto 6 (repetido) — desempenho ✅

`SUM(value)`, 2.ª execução: lakeFS 0.007 s vs local 0.001 s. Diferença de ~6 ms para 3
ficheiros (≈2 ms/ficheiro), o que confirma custo fixo por pedido. A 1.ª execução (0.031 s)
inclui o aquecimento de ligações. A ressalva anterior mantém-se: 3 ficheiros em localhost
não dizem nada sobre milhares de ficheiros ou concorrência.

### Ponto 9 — duas refs na mesma consulta ✅

Uma única consulta lê a tag `gold-v0.1.0` e o branch `etl-gap` (de onde se apagou a
partição de 2026-03-02), agrega por `dt` e junta com `FULL OUTER JOIN`:

| dt | linhas_tag | linhas_branch | diferença |
|---|---|---|---|
| 2026-03-01 | 33760 | 33760 | 0 |
| 2026-03-02 | 33120 | NULL | 33120 |
| 2026-03-03 | 33120 | 33120 | 0 |

O dia em falta é detetado. **Armadilha confirmada:** o lado ausente vem `NULL`, não `0`;
a coluna `diferença` só dispara porque a consulta aplica `coalesce`. Um gate escrito como
`linhas_tag <> linhas_branch` sem tratar `NULL` deixaria passar o dia em falta.

## Aprendizagens laterais

- **Timestamps em Parquet** são `INT64` mais uma anotação de unidade e ajuste a UTC; o valor
  cru `1772323260000` é epoch em **milissegundos** = `2026-03-01 00:01:00 UTC`. Lido como
  segundos daria o ano 58132, como microssegundos daria 1970 — nenhuma das leituras dá erro.
  Reforça a decisão do plano de manter `source_ts` e `ingest_ts` separados no bronze e
  normalizar para UTC no silver.
- **Modo `--quickstart` guarda tudo dentro do container.** `docker stop`/`start` é seguro;
  `docker rm` apaga o repositório. Arrancar sempre com `-d`.

## Decisão

**Seguir com o desenho Write-Audit-Publish da §2 do plano.** Todos os pontos do spike
foram respondidos com evidência e nenhum contradiz o desenho:

- branches isolam dados por validar; só o merge promove;
- o DuckDB lê qualquer ref (branch, tag) pelo gateway S3, com glob e partições Hive;
- o custo do gateway é fixo por ficheiro e os dados chegam intactos;
- a reconciliação entre duas refs (gate G3) é implementável numa única consulta.

Regras que passam para o desenho dos gates:
1. Qualquer comparação entre refs trata o lado ausente (`coalesce`), porque a ausência é
   `NULL` e `NULL <> x` não é verdadeiro.
2. O passo de commit é explícito no DAG: staging não é histórico.
3. Repetir a medição de desempenho a escala realista antes de fechar a Fase 4.

Alteração ao plano: o armazenamento passou de MinIO para SeaweedFS (ADR-002).
