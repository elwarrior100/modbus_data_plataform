# ADR-022: BI e dashboard live (Fase 7)

**Estado:** aceite (2026-11-23): L1, S1, B1, P1, com as condições de uma revisão independente (secção 7). L1 em curso.
**Liga a:** PLAN Fase 7 (e o que falta da Fase 3: o caminho live para o Timescale), §9 (orçamento de memória);
ADR-006 (ingestão e spool), ADR-019 (gold e `LATEST_STABLE`), ADR-020 (crescimento).

## 1. O que a Fase 7 tem de provar (DoD do PLAN)
1. Uma alteração no simulador aparece no Grafana em **< 5 s**, e a latência é **medida**, não estimada.
2. O BI responde a perguntas de negócio ("consumo específico da bomba por turno") **sem SQL ad-hoc**.
3. Um dashboard executivo de KPIs e um de qualidade.
4. O BI lê o gold a partir do **`LATEST_STABLE`** (uma release), nunca "o mais recente".

## 2. O que já existe (medido a 2026-11-23 na Oracle)
| | |
|---|---|
| Memória | 7,5 de 10,6 GiB alocáveis em uso: **~3 GiB livres** (picos de 1–1,5 GiB ao domingo: GC + ensaio) |
| Grafana + Prometheus | já instalados (380 Mi + 604 Mi), dashboards da plataforma |
| PostgreSQL | imagem `timescale/timescaledb` pg17: a extensão **TimescaleDB 2.32 está disponível** e não instalada |
| Spool (`ingest.events`) | todas as amostras, ~1 s depois da leitura, guardadas 7 dias; ~52 600 por hora |
| Gold | 9 tabelas por dia, ~0,4 MB, com release e `LATEST_STABLE` (ADR-019) |
| Mais pesados | OpenMetadata + OpenSearch ~1,9 GiB |

## 3. Decisões e opções

### D1 — Caminho live (o que fica da Fase 3): de onde o Grafana lê "agora"
| Opção | Como | Latência | Memória | Prós / contras |
|---|---|---|---|---|
| **L1 — hypertable alimentada por trigger na spool (R)** | um trigger em `ingest.events` (INSERT) descodifica o nó (ativo, tag) e o valor e escreve numa hypertable `live.readings`; compressão ao fim de 1 dia e retenção de 7 dias (políticas do Timescale) | ms depois da spool (~1–2 s ponta a ponta) | ~0 (dentro do Postgres) | sem processo novo; o Grafana lê uma tabela limpa e própria. Contra: o trigger corre em cada INSERT da spool (~15/s; medir o efeito no collector e no WAL) |
| L2 — o collector escreve também na hypertable | o collector descodifica e grava em dois sítios | ~1 s | ~0 | mexe no collector e na sua idempotência (ADR-006); duas escritas por amostra |
| L3 — processo pequeno que copia da spool de segundo a segundo | um pod novo lê `events` pelo `seq` e escreve na hypertable | ~1–2 s | ~60–100 MB | desacoplado; mais um serviço para manter |
| L4 — o Grafana lê a spool diretamente (uma vista) | sem tabela nova | ~1 s | 0 | o mais barato, mas os dashboards ficam presos ao esquema interno da spool e às consultas pesadas numa tabela que está sempre a receber escritas |

### D2 — Gold para o PostgreSQL (o que o BI lê)
| Opção | Como | Rollback |
|---|---|---|
| **S1 — por release, com vista para a atual (R)** | depois da release, a `gold_daily` copia as tabelas do dia para `serving.gold_<tabela>` com uma coluna `release`; uma vista `gold.<tabela>` mostra só as linhas das releases do caminho do `LATEST_STABLE` | o rollback do ponteiro muda a vista (uma linha numa tabela `serving.latest_stable`): **instantâneo**, sem copiar |
| S2 — substituir ao mover o ponteiro | cada vez que o ponteiro muda, apaga e recopia o gold | um rollback recopia (segundos, mas copia dados) |
| S3 — o BI lê o gold direto do lakeFS (DuckDB / S3) | sem cópia | o Superset não lê Parquet no S3 sem um motor à frente (Trino/DuckDB servido): mais um serviço |

Tamanho: ~0,4 MB de gold por dia → ~150 MB por ano no Postgres (dentro dos backups atuais; os dumps crescem ~0,1 GB/ano).

### D3 — A ferramenta de BI (o "sem SQL ad-hoc")
| Opção | Memória | O que dá | Contra |
|---|---|---|---|
| **B1 — Superset, modo mínimo (R)** | ~500–800 MB (servidor web com 1–2 workers, sem Celery nem Redis; metadados no Postgres existente) | camada semântica: datasets com métricas definidas uma vez ("kWh/m³ = Σ energia ÷ Σ volume"), gráficos e filtros sem SQL; é o que o PLAN e o D13 pedem | o serviço novo mais pesado da fase; cabe nos ~3 GiB, mas os picos de domingo ficam mais perto do limite → **ligar só um perfil por vez** (ver D5) |
| B2 — só Grafana | 0 a mais | já está; ótimo para tempo real e KPIs fixos | sem exploração: cada pergunta nova é um painel com SQL. Não cumpre bem o "sem SQL ad-hoc" |
| B3 — Metabase | ~700 MB–1 GB (Java) | perguntas sem SQL muito simples | mais pesado que o Superset; outra stack (Java) |

### D4 — Medir a latência (DoD 1)
**Recomendação única:** um teste `tests/phase7/latency.py` que (1) ativa um `spike` num sinal pelo HTTP do simulador
com um valor marcado, (2) pergunta à API do Grafana (a mesma consulta do painel) de 200 em 200 ms até o valor
aparecer, e (3) regista o tempo, 20 vezes, com mediana e p95. O resultado fica no PLAN e no dashboard de qualidade.

### D5 — Memória e perfis (PLAN §9)
| Opção | O que corre |
|---|---|
| **P1 — tudo ligado, com limites apertados e medido uma semana (R)** | Superset com limite de 768 Mi; se o pico de domingo passar de ~90% da memória, passa-se a P2 |
| P2 — OpenMetadata só quando preciso | o perfil `core+bi` do PLAN: OpenMetadata e OpenSearch (~1,9 GiB) parados por omissão e ligados para ver o catálogo; o envio do gold/G2 é "melhor esforço" e não falha |
| P3 — Superset só quando preciso | parado por omissão (`replicas: 0`), ligado para explorar |

### D6 — Dashboards como código
**Recomendação única:** dashboards do Grafana e do Superset no git (JSON/YAML exportado), aplicados pelo Argo CD
(o Grafana já carrega datasources por ficheiro). Nada criado à mão na interface.

## 4. Custo
Todas as opções: **€0**. Tudo corre na VM (nada novo na OCI); o Postgres cresce ~150 MB/ano com o gold e a hypertable
live fica limitada a 7 dias comprimidos (~0,2–0,5 GB, a medir); nenhum pedido novo ao Object Storage além dos dumps
um pouco maiores. O risco é só de **memória** (D5), não de dinheiro.

## 5. Escolhas do Stéphane
1. Caminho live: **L1** (trigger na spool, recomendado), L2, L3 ou L4?
2. Gold no Postgres: **S1** (por release, rollback instantâneo, recomendado), S2 ou S3?
3. BI: **B1 Superset** (recomendado), B2 só Grafana, ou B3 Metabase?
4. Memória: **P1** medir com tudo ligado (recomendado), P2 OpenMetadata a pedido, ou P3 Superset a pedido?

## 6. Ordem proposta (depois das escolhas)
1. L1 no cluster local + o teste de latência (fecha a Fase 3 e o DoD 1 da Fase 7).
2. S1 na `gold_daily` + a vista do `LATEST_STABLE`.
3. Dashboards live e de qualidade no Grafana (como código).
4. Superset (B1) com os datasets e as métricas; o dashboard executivo; a pergunta do DoD respondida sem SQL.
5. Uma semana de medição de memória na Oracle (D5), e só então dar a fase por fechada.

## 7. Escolhas (2026-11-23) e condições da revisão independente
O Stéphane aceitou as recomendações **L1, S1, B1 e P1**, desde que um revisor independente as aceitasse. O revisor
(um agente, só leitura na Oracle e no código) deu **aceito com condições**, com estes factos medidos na Oracle às 20:43:
- **memória:** 7 931 Mi usados (73 %), ~2,7 GiB livres e não ~3. Os *limits* somam 148 % do nó; 148 Mi em swap;
- **spool:** ~15 INSERT/s, ~6 linhas por transação, `raw_value` em JSON de tipos mistos; o WAL de todas as bases cresce ~5,5 GB/dia;
- **Postgres:** `max_worker_processes` = 8, abaixo do que o Timescale precisa;
- **Grafana:** o plugin PostgreSQL estava desligado, o refresh mínimo é 5 s e a NetworkPolicy não o deixava chegar ao Postgres.

### 7.1 L1: condições e como ficaram cumpridas
| Condição | Como |
|---|---|
| o trigger nunca faz falhar o INSERT do collector | corpo em `BEGIN … EXCEPTION WHEN OTHERS` → `RAISE WARNING` + contador `live.health.failures` |
| não esperar por bloqueios da compressão/retenção | `SET lock_timeout = '100ms'` na função |
| converter só o que se sabe converter | tabela `live.nodes` gerada do `nodes.yaml` em cada arranque do collector (o teste compara-a com o ficheiro); números → double, booleanos → 1/0, `device_clock` fica de fora |
| tempo `NOT NULL` com a regra `clock_skew` | `source_ts`, ou `server_ts` se diferirem > 60 s (a regra da silver), ou `ingest_ts` |
| `live` fora dos dumps; restauro ensaiado | `pg_dump --exclude-extension=timescaledb -N live -N '_timescaledb*' -N 'timescaledb_*'`; ensaiado no cluster local: o restauro passa, o trigger fica e um INSERT na base restaurada entra (com um aviso) |
| `max_worker_processes` ≥ 12, telemetria desligada | `max_worker_processes=16`, `timescaledb.max_background_workers=4`, `timescaledb.telemetry_level=off` |
| falhar de propósito não perde nem repete amostras | ensaiado no cluster local: com a hypertable renomeada durante 60 s, a spool recebeu 885 amostras (o ritmo normal), 0 `event_id` repetidos, 705 falhas contadas, nenhum erro no collector; ao repor, o live voltou sozinho |
| latência < 5 s medida no ecrã, não só nos dados | Grafana com `min_refresh_interval = 1s`, painel "agora" a 1 s; o teste soma 1 s (o pior caso do refresh) ao p95 |

Implementado:
- `ingest/src/ingest/live.py` com a hypertable, a tabela de nós, o trigger e a compressão/retenção. O collector chama `try_install` ao arrancar, em melhor esforço;
- os testes `ingest/tests/test_live.py` (8, contra o TimescaleDB real);
- `tests/phase7/latency.py` e `tests/phase7/run.sh`.

Na plataforma:
- a extensão, o esquema `live` e o papel `grafana_live`, que só lê o esquema `live`, em `bootstrap-postgres-ingest.sh`;
- os argumentos do Postgres, os dumps, o Grafana e a NetworkPolicy;
- o dashboard `projects/modbus/k8s/ingest/dashboard-live.yaml`.

**Medido (cluster local, 23-11):** caminho dos dados (do simulador até à hypertable, `--via sql`, 5 corridas): mediana
**1,97 s**, máximo **2,00 s**. **Pelo Grafana** (plataforma 6ecbe99, 20 corridas, a mesma consulta do painel): dados mediana
**1,98 s**, p95 **2,02 s**, máximo 2,03 s; no ecrã (+1 s do refresh, pior caso) p95 **3,02 s** → **DoD 1 cumprido no cluster local**
(< 5 s). Falta repetir na Oracle depois da promoção.

### 7.2 S1: condições e implementação (no disco, por rever)
`pipelines/src/gold/serving.py`:
- **Esquema na base `serving`:** `serving.releases` (com o carimbo numérico), `serving.latest_stable` (a cópia do
  ponteiro), `serving.gold_<tabela>` (com a coluna `release`), `serving.visible_releases` e as vistas `gold.<tabela>`.
- **`sync`:** idempotente. Carrega as releases do lakeFS que faltem e copia o ponteiro, por isso corrige qualquer divergência.
- **`check`:** só compara, sem corrigir.

Ligações a outros sítios:
- `gold.release.point`, o caminho de cada release e de cada rollback, chama `publish_pointer` em melhor esforço;
- o `gold_daily` ganha a tarefa `serve` depois da release, com `sync` + `check`, e falha se o Postgres divergir;
- na plataforma: a base e o papel `serving` (`bootstrap-postgres-serving.sh`) e o segredo `airflow/serving-db`.

Testes `pipelines/tests/test_gold_serving.py` (5, contra o Postgres real):
- as vistas seguem o ponteiro;
- o rollback muda as vistas sem copiar nada, em < 1 s;
- o `check` encontra uma divergência e o `sync` corrige-a;
- a ordem é pelo carimbo (v1.10 depois de v1.9);
- recarregar uma release substitui-a.

Pipelines: 112 passam, 19 saltados.

Nota de semântica: uma release com G3 `warn` criada antes do ponteiro passa a ver-se quando o ponteiro avança para
uma release posterior. É coerente com o lakeFS, onde o `main` dessa release já a contém.

As condições:
1. A vista escolhe, para cada dia, a release mais recente que não passa o ponteiro, por um **ordinal numérico**
   (texto semver ordena mal: v1.10 < v1.9). As `dim_*` copiam-se inteiras em cada release.
2. `serving.latest_stable` é uma segunda cópia do ponteiro do lakeFS: o `gold.release rollback` atualiza as duas, e uma
   verificação alerta se divergirem.
3. O rollback em < 1 min é cronometrado também no Postgres.

### 7.3 B1: condições (por fazer)
1 worker gunicorn gthread; pico medido no cluster local (incluindo o `db upgrade`/`init`); `SECRET_KEY` pelo External
Secrets; papel só de leitura limitado a `gold`/`serving` (nunca a spool); NetworkPolicy própria; Redis, Postgres e
worker do chart desligados.

### 7.4 P1: condições (por fazer)
- **O limite dos 90 %:** é o *working set* sobre o *allocatable* (10 856 Mi), medido pelo Prometheus durante 7 dias.
- **Swap:** alerta se passar de 1 GiB.
- **P2 pronta antes de ligar o Superset:** o OpenMetadata a pedido.
- **Requests do Superset:** iguais ao uso real.

Estimativa do revisor: 7,9 GiB + o pico de domingo + o Superset ≈ 94 %, por isso o mais provável é acabar em P2. Nunca
se cresce a VM (custo €0).
