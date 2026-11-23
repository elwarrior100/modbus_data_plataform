# Plano — Plataforma de Dados Industrial (Modbus → OPC UA → Lakehouse → BI/ML)

> Regras de colaboração: ver [`../CLAUDE.md`](../CLAUDE.md). O Claude **não escreve código**
> neste projeto. Este documento é a fonte de verdade do âmbito e da sequência.

**Data:** 2026-08-25 · **Rev:** 3 (stack alinhada com os serviços do projeto do Gabriel:
PostgreSQL, lakeFS, OpenMetadata, Airflow) · **Estado:** Fase 0 por iniciar
**Host:** MacBook (12 cores, 36 GB RAM, ~690 GB livres)

---

## 1. Objetivo

Construir, **inteiramente on-premise**, uma plataforma de dados que:

1. Simula equipamento industrial a falar **Modbus RTU sobre RS-232** (e Modbus TCP).
2. Converte a camada OT para **OPC UA** — a fronteira normalizada entre chão de fábrica e IT.
3. Ingere para um **lakehouse medallion** (bronze → silver → gold) em object storage
   versionado com **lakeFS**.
4. Aplica **gates de qualidade** que bloqueiam a promoção de dados maus — literalmente,
   via *hooks* de pré-merge do lakeFS.
5. Publica gold com **tag de release imutável** (tag lakeFS + manifest).
6. Governa tudo no **OpenMetadata**: catálogo, linhagem, glossário e resultados de qualidade.
7. Serve **BI** e um **dashboard live**, mais um produto final apresentável.
8. Fecha com **machine learning de séries temporais** (anomalia + previsão de falha),
   treinado a partir de uma tag lakeFS — logo, reproduzível ao byte.

Os três objetivos pessoais — portfólio, aprendizagem profunda, sem prazo apertado —
implicam uma consequência de método: **cada fase termina com uma coisa a funcionar
ponta-a-ponta**, e nenhuma fase é dada como fechada sem os critérios de aceitação cumpridos.

---

## 2. A ideia central: Write-Audit-Publish

O lakeFS dá ao *data lake* a semântica do git: branches, commits, tags imutáveis, merge e
*hooks*. Isso transforma "gates de qualidade" e "tag de release" de convenções frágeis num
mecanismo **imposto pela infraestrutura**:

```
main ────●────────────────────────────────●──────────► (tag: gold-v1.3.0+202608251400)
          \                              /
           ●──►●──►●──► [quality gates] ─┘ merge só se os gates passarem
        branch: etl-2026-08-25T14
```

1. **Write** — o Airflow abre um branch (`etl-<batch_id>`) e escreve lá bronze, silver e gold.
   O `main` **nunca** vê dados por validar.
2. **Audit** — os gates correm contra o branch: são dados reais, num sítio real, sem risco.
3. **Publish** — o merge para `main` é tentado; um *pre-merge hook* do lakeFS lê o resultado
   dos gates e **rejeita o merge** se falharem. O branch mau fica em quarentena, disponível
   para autópsia, sem ter contaminado nada.
4. **Tag** — um merge bem-sucedido em gold produz uma tag imutável = a release.

Consequências diretas: rollback é repontar para a tag anterior (segundos); reprodutibilidade
de um treino de ML é o hash do commit; e "quem estragou o quê, quando" é um `lakectl log`.

---

## 3. Arquitetura alvo

```
┌─ OT (simulado, dentro da VM Linux) ─────────────────────────────┐
│  Simulador de ativos          par série virtual (socat)         │
│  bomba · motor · chiller  ──►  /dev/ttyV0 ⇄ /dev/ttyV1          │
│  registos 16-bit, uint32/float32 em pares de registos           │
│                                    │ Modbus RTU (RS-232)        │
│                                    │ + Modbus TCP (:502)        │
└────────────────────────────────────┼────────────────────────────┘
                                     ▼
                        ┌────────────────────────┐
                        │  Gateway Modbus→OPC UA │  address space modelado:
                        │  (servidor OPC UA)     │  objetos por ativo, EU, EURange
                        └───────────┬────────────┘
                                    │ OPC UA subscriptions (deadband)
                                    ▼
┌─ IT · k3s numa VM Linux (Lima) ────────────────────────────────────────────┐
│                                                                            │
│  Collector OPC UA ──┬──► spool local ──► writer bronze (branch lakeFS)     │
│                     └──► Postgres/Timescale (caminho live, baixa latência) │
│                                                                            │
│   ┌────────────────────────────────────────────────────────────────────┐   │
│   │  lakeFS  (versionamento: branches · commits · tags · hooks)        │   │
│   │      └── SeaweedFS (S3 on-prem, storage físico)                        │   │
│   │            bronze/  raw imutável, particionado por dia/hora        │   │
│   │            silver/  parquet canónico, tipado, deduplicado          │   │
│   │            gold/    agregados de negócio + features ML             │   │
│   └────────────────────────────────────────────────────────────────────┘   │
│                                                                            │
│  Airflow (KubernetesExecutor) orquestra: branch → ETL → gates → merge → tag│
│  OpenMetadata: catálogo · linhagem · glossário · testes de qualidade       │
│  Postgres: metadata Airflow + lakeFS KV + OpenMetadata + serving layer     │
│  MLflow: tracking + registry (artefactos no SeaweedFS, dados por tag lakeFS)   │
│                                                                            │
│  Grafana (live, <5 s)   ·   Superset (BI sobre gold)                       │
│  Prometheus + Loki (observabilidade)   ·   Argo CD (GitOps)                │
└────────────────────────────────────────────────────────────────────────────┘
```

**Porquê OPC UA no meio:** Modbus não tem tipos, nem unidades, nem qualidade, nem
timestamps — é um mapa de inteiros de 16 bits. OPC UA tem tudo isso. É exatamente aí que
nasce metade dos problemas de qualidade de dados industriais, e é isso que torna o projeto
interessante em vez de "mais um pipeline".

---

## 4. Decisões técnicas (registo)

Cada decisão tem um código (D1…D15) e é registada num ADR em `docs/adr/`. Os números dos ficheiros ADR são outra
sequência (por ordem de escrita): a última coluna diz em que ADR está cada decisão.

| # | Decisão | Escolha | Alternativas | Porquê | ADR |
|---|---|---|---|---|---|
| D1 | Runtime k8s | **k3s numa VM Linux (Lima)** | k3d em Docker, Pi cluster | k3s a sério, storage e rede realistas, reprodutível | [001](adr/001-runtime-kubernetes.md) |
| D2 | Object storage | **SeaweedFS** (MinIO deixou de publicar imagens; ver ADR-002) | MinIO, Ceph | API S3, Apache-2.0, leve | [002](adr/002-armazenamento-s3.md) |
| D3 | Versionamento de dados | **lakeFS** *(requisito)* | Iceberg, Delta, nada | Branches, tags imutáveis e hooks pré-merge — é o motor dos gates e das releases | [000](adr/000-spike-lakefs-duckdb.md), [006](adr/006-ingestao-e-bronze.md) |
| D4 | Formato de tabela | **Parquet + partições Hive** | Iceberg, Delta | O lakeFS já dá atomicidade, time travel e histórico. Somar Iceberg duplicaria conceitos e triplicaria a fricção de integração | [000](adr/000-spike-lakefs-duckdb.md) |
| D5 | Catálogo e governança | **OpenMetadata** *(requisito)* | DataHub, Amundsen | Catálogo, linhagem, glossário, testes de qualidade e profiler num só sítio | [009](adr/009-openmetadata.md) |
| D6 | Motor de qualidade | **OpenMetadata Data Quality** + hook lakeFS a impor | Soda, Great Expectations | Uma ferramenta só; resultados já aterram no catálogo, com histórico | [008](adr/008-gates-e-imposicao.md) |
| D7 | Fronteira OT/IT | **Servidor OPC UA** | Modbus TCP direto | Tipos, unidades, quality codes, subscriptions | [004](adr/004-gateway-opcua.md) |
| D8 | Base de dados | **PostgreSQL** *(requisito)* + TimescaleDB | ClickHouse, DuckDB servido | Backend de Airflow, lakeFS, OpenMetadata **e** serving layer. Timescale para o caminho live | [006](adr/006-ingestao-e-bronze.md) |
| D9 | Transporte | **Sem broker na v1** (spool local no collector) | Redpanda/Kafka, NATS | Ver §4.1 | [006](adr/006-ingestao-e-bronze.md) |
| D10 | Engine de transformação | **DuckDB** (lê via gateway S3 do lakeFS) | Spark, Polars, Trino | O volume não justifica Spark; DuckDB lê Parquet no S3 depressa e cabe em qualquer pod | [000](adr/000-spike-lakefs-duckdb.md), [007](adr/007-airflow-e-silver.md) |
| D11 | Orquestração | **Airflow 3, KubernetesExecutor** *(requisito)* | Dagster, Prefect | Confirmado na stack de referência; integra com lakeFS (hooks/operadores) e alimenta a linhagem do OpenMetadata | [007](adr/007-airflow-e-silver.md) |
| D12 | Dashboard live | **Grafana** (fonte: Timescale) | app própria | Refresh curto, alerting incluído | (Fase 7) |
| D13 | BI | **Superset** | Metabase, só Grafana | Camada semântica, exploração ad-hoc sobre gold | (Fase 7) |
| D14 | ML tracking | **MLflow** | W&B (cloud → viola on-prem) | On-prem, registry, artefactos no SeaweedFS | (Fase 8) |
| D15 | GitOps | **Argo CD** | Flux, kubectl à mão | Estado declarativo, drift detection | [005](adr/005-gitops-argocd.md), [010](adr/010-repos-gitops.md), [012](adr/012-promocao-entre-ambientes.md) |

> **Regra:** antes de escrever o ADR, o Stéphane defende a escolha em duas frases. Se não
> conseguir defender, não está pronto para decidir — investiga primeiro.

### 4.1 Nota sobre o broker (decisão D9, ADR-006)

A lista de serviços do projeto de referência não inclui broker, e o OpenMetadata traz
consigo um custo de memória considerável (servidor + índice de pesquisa). Somar Kafka na v1
é gastar ~3 GB e uma semana de integração num problema que ainda não existe.

**Decisão:** v1 sem broker. O collector escreve num *spool* local em disco (durável, com
confirmação após persistência) e alimenta dois caminhos: micro-batch para bronze e escrita
direta para Timescale (live). O broker fica documentado como extensão da Fase 9, com o
gatilho explícito para o adotar: **um terceiro consumidor**, ou necessidade de replay
independente do lake. Adotar por esse motivo é arquitetura; adotar antes é currículo.

---

## 5. Modelo de dados

### 5.1 Cenário simulado

Três ativos, ~15 sinais, taxa base 1 Hz (configurável até 10 Hz):

| Ativo | Sinais |
|---|---|
| `PUMP-01` | pressão de saída (bar), caudal (m³/h), corrente (A), temperatura da chumaceira (°C), vibração RMS (mm/s), horas de funcionamento (contador uint32), estado (enum), alarme (coil) |
| `MOTOR-01` | rpm (uint32), binário (Nm), corrente (A), temperatura do enrolamento (°C), vibração RMS (mm/s) |
| `CHILLER-01` | temperatura de entrada/saída (°C), potência (kW), estado (enum) |

**Detalhes deliberados de realismo** (fonte dos problemas de qualidade):
- Valores 32-bit (`uint32`, `float32`) ocupam **dois registos de 16 bits** com ordem de
  palavras configurável (big/little endian + word swap). Ler mal = valores absurdos.
- Escalas inteiras: temperatura em décimas de grau (`235` = 23.5 °C).
- *Scan rate* diferente por sinal.
- O clássico off-by-one Modbus: `40001` vs offset `0`.

**Cenários injetáveis** (cada um tem de ser detetável a jusante): `normal`, `drift`
(degradação lenta de rolamento ao longo de dias), `sensor_stuck`, `spike`, `noise_burst`,
`comms_gap`, `clock_skew`, `power_cycle` (reset do contador de horas).

### 5.2 Bronze — cru e imutável

Envelope, sem interpretação semântica. Uma linha por amostra recebida.

| Campo | Descrição |
|---|---|
| `event_id` | UUID determinístico (hash do payload + source) para dedup idempotente |
| `source` | identificador do collector/gateway |
| `opcua_node_id` | node id de origem |
| `raw_value` | valor tal como veio |
| `opcua_status_code` | quality code OPC UA, sem tradução |
| `source_ts` | timestamp do dispositivo/servidor |
| `ingest_ts` | timestamp de receção (UTC) |
| `schema_version`, `batch_id` | proveniência |

Particionado por `dt=YYYY-MM-DD/hour=HH`. **Nunca reescrito, nunca corrigido** — e com
lakeFS isto deixa de ser disciplina para passar a ser verificável no histórico de commits.

### 5.3 Silver — canónico, tipado, longo

Uma linha por (ativo, tag, instante). Formato longo porque tags aparecem e desaparecem —
formato largo obrigaria a alterar o schema a cada sensor novo.

| Campo | Descrição |
|---|---|
| `asset_id`, `tag_name` | chave de negócio |
| `ts_utc` | timestamp normalizado UTC |
| `value_double` / `value_bool` / `value_str` | valor tipado, desescalado e convertido |
| `unit` | unidade de engenharia (vinda do OPC UA) |
| `quality` | enum normalizado: `GOOD` / `UNCERTAIN` / `BAD` (+ razão) |
| `ingest_ts`, `bronze_event_id`, `bronze_commit` | linhagem para trás, até ao commit lakeFS |
| `dq_flags` | flags detetadas (stuck, out_of_range, late, duplicate) |

Regras: dedup por `event_id`; late-arriving aceite até 24 h com reprocessamento da partição
afetada; unidades convertidas para SI; **nunca apagar valores maus — marcá-los**.

### 5.4 Gold — negócio e features

**BI** — `fact_asset_minute`, `fact_asset_hour`, `dim_asset`, `dim_tag`, `fact_downtime`.
KPIs: disponibilidade, MTBF, MTTR, consumo específico (kWh por m³ bombeado), taxa de alarmes.

**ML** — `feature_asset_5min`: lags (1/5/15/60 min), rolling mean/std, deltas, taxa de
variação, tempo desde último alarme, energia de vibração. **Point-in-time correcto**:
nenhuma feature pode usar informação futura relativa a `ts_utc`.

---

## 6. Gates de qualidade

Três checkpoints. Um gate que falha **não corrige** — impede o merge.

| Gate | Onde | Verifica |
|---|---|---|
| **G1 — Contrato** | collector → bronze (branch) | node ids conhecidos, tipos esperados, payload parseável, `source_ts` plausível |
| **G2 — Integridade silver** | branch, antes do merge | completude (amostras recebidas vs esperadas pelo scan rate), unicidade (asset+tag+ts), validade (dentro de `EURange`), frescura, % `quality != GOOD` sob limiar, deteção de *stuck sensor* |
| **G3 — Coerência gold** | branch, antes do merge e da tag | reconciliação de totais com silver, sem buracos temporais, KPIs dentro de fronteiras físicas, drift estatístico (PSI/KS), contagem de linhas dentro de ±X% |

**Execução:** test suites do OpenMetadata (mais regras próprias onde o OpenMetadata não
chega, ex. reconciliação entre camadas). Os resultados ficam no catálogo, com histórico e
visíveis na página de cada tabela.

**Imposição:** *pre-merge hook* do lakeFS. O merge é uma operação atómica que **falha** se o
gate falhar. É a diferença entre um pipeline que "verifica a qualidade" e um que a garante.

**Severidades:** `blocker` (bloqueia o merge), `warn` (merge passa, alerta dispara),
`info` (só regista). Regra para escolher: se o dado errado chegar ao utilizador final e o
levar a decidir mal, é `blocker`.

---

## 7. Release tag do gold

Um merge bem-sucedido em `main` que altere gold gera uma **release imutável**:

- Tag lakeFS: `gold-vMAJOR.MINOR.PATCH+YYYYMMDDHHmm` sobre o commit exato.
- Manifest (versionado no próprio repo lakeFS): contagens por tabela, intervalo temporal
  coberto, commit git do código produtor, versão dos DAGs, resultados de G2/G3, e o commit
  lakeFS de bronze que a originou.
- Semântica: `MAJOR` = quebra de schema · `MINOR` = tabela/coluna nova compatível ·
  `PATCH` = reprocessamento sem alteração de schema.
- Consumidores (Superset, Grafana, treino de ML) apontam para **uma tag**, nunca para
  "o mais recente". O ponteiro `LATEST_STABLE` só avança depois de G3 passar.
- **Rollback**: repontar `LATEST_STABLE` para a tag anterior. Critério de aceitação: menos
  de um minuto, sem copiar dados.
- No OpenMetadata, a release aparece como *tag* nas entidades gold — o catálogo mostra em
  que versão está cada tabela e qual a sua linhagem até bronze.

---

## 8. Fases

Cada fase tem **Definition of Done** verificável. Não avança sem DoD cumprido.
Nunca duas fases abertas ao mesmo tempo.

### Fase 0 — Fundações  ✅ concluída (2026-10-24)
Repo git, estrutura de pastas, ADRs 001-004, VM Linux (Lima) dimensionada
(8 vCPU / 24 GB RAM / 150 GB disco), k3s, `kubectl` a falar a partir do macOS, namespaces,
ingress, storage class, SeaweedFS, PostgreSQL, **lakeFS** (KV backend em Postgres, storage em
SeaweedFS), repositório lakeFS `industrial-lake` inicializado com `main`, registry local.
**DoD:** `kubectl get nodes` do macOS mostra Ready; SeaweedFS e a UI do lakeFS abrem no browser;
um ficheiro escrito num branch lakeFS **não** aparece em `main` antes do merge, e aparece
depois; tudo sobrevive a reboot da VM.

### Fase 1 — OT: Modbus + RS-232  ✅ concluída (2026-10-25)
Simulador dos três ativos, **mapa de registos documentado em markdown** (entregável por
direito próprio: endereço, tipo, escala, unidade, word order), par série virtual, Modbus RTU
sobre série + TCP, cenários injetáveis por configuração.
**DoD:** cliente Modbus externo lê por RS-232 e por TCP; `uint32` lido com a ordem de
palavras correta; ativar `sensor_stuck` produz efeito observável; o mapa de registos está
completo e correto.

### Fase 2 — Gateway OPC UA  ✅ concluída (2026-10-25)
Cliente Modbus → servidor OPC UA com address space modelado por ativo, `EngineeringUnits`,
`EURange`, e mapeamento de falhas Modbus para *status codes* (timeout → `BAD_...`, **não**
`GOOD` com o valor antigo — é o erro clássico e é uma fonte silenciosa de dados falsos).
**DoD:** UaExpert navega a árvore de ativos; subscription com deadband entrega alterações;
desligar o simulador faz as variáveis passarem a `BAD` em segundos.

### Fase 3 — Ingestão e bronze (Write)  🟡 implementada, DoD parcial (2026-11-08)
Collector OPC UA (subscriptions, reconnect com backoff, spool local durável), writer que
abre branch lakeFS, escreve bronze particionado e faz commit. Gate G1. Caminho live para
Timescale em paralelo.
**DoD:** 24 h contínuas sem perda; matar o writer a meio e reiniciar não duplica nem perde
(verificado por contagem de `event_id` distintos); G1 rejeita um payload malformado injetado
de propósito e o branch correspondente **não** chega a `main`.

> **G1 provado no cluster (2026-11-21, cluster local):** uma amostra inválida injetada na spool (nó desconhecido,
> `event_id` que não se recalcula) fez o writer REJEITAR o lote `be1809225aa6b955` (G1-02 e G1-03). O relatório
> ficou em `bronze/_rejected/<lote>/g1_report.json` no branch dele; nenhum commit do `main` refere o lote. A
> reconciliação a seguir deu 0 em falta, 0 a mais e 0 duplicados (1,48 M de amostras). Falta, para fechar o DoD: o
> caminho *live* para o Timescale (as 24 h contínuas já se cumprem na Oracle desde 14-11).
>
> **Estado real (2026-11-08).** Feito e verificado: collector OPC UA, spool, writer idempotente,
> gate G1 (8 regras) e reconciliação spool↔lakeFS sem faltas nem duplicados no cluster
> (27 116 amostras), incluindo mortes do writer e um reinício do Postgres. **Desvios:** o spool é
> PostgreSQL e não um ficheiro (ADR-006); o lote tem 3 estados. **Em falta para fechar o DoD:**
> as 24 h contínuas, o G1 provado no cluster com uma amostra inválida (só o está contra o lakeFS
> real, num destino descartável), e o caminho *live* para o Timescale. **Adiado:** comandos
> para listar e reprocessar lotes rejeitados, e G1 por linha em vez de por lote.

### Fase 4 — Silver + Airflow  ✅ concluída (2026-11-09)
Airflow no k3s (KubernetesExecutor, Postgres), DAG que abre branch → bronze→silver →
commit → merge. Desescalamento, conversão de unidades, dedup, normalização de quality,
backfill e late-arriving.
**DoD:** backfill de 7 dias produz resultado idêntico ao incremental (idempotência);
reprocessar a mesma partição duas vezes não altera contagens; uma falha a meio do DAG deixa
`main` intacto (o trabalho por acabar fica no branch) — este é o teste que prova o WAP.

> **Desvios decididos (ADR-007, 2026-11-08).** A silver é particionada pela hora de **receção**
> (1 para 1 com o bronze), não pela hora do evento: dispensa reprocessar partições antigas com
> dados atrasados. As unidades **não** se convertem para SI: mantêm-se as do gateway, com código
> UNECE e um catálogo de conversão (hoje identidade). Contrato: `docs/silver-contract.md`.
>
> **Estado (2026-11-09):** DoD verificado: backfill de 7 dias == incremental, byte a byte (169 ficheiros,
> `test_week.py`); reprocessar uma hora não altera nada (testes locais e contra o lakeFS real, 8 casos);
> falha em cada um dos 5 passos antes do merge deixa o `main` intacto (WAP). DAG `silver_hourly` no cluster,
> horas 16-18 de 07-11 reconciliadas, catchup de 41 execuções sem erros. Retenção de branches: DAG
> `lake_housekeeping` (diário, em simulação até rever o primeiro relatório; ver ADR-007).
> Por fazer, fora do DoD: GC de objetos órfãos do lakeFS (precisa de Spark; só se o armazenamento o justificar).

### Fase 5 — Governança e gates  🟢 DoD cumprido no cluster local (2026-11-21); falta levar o catálogo à Oracle

> **DoD provado (21-11, cluster local):**
> - **`spike`:** PUMP-01.bearing_temp a 500 °C durante 30 s, às 19:10. A hora das 19 teve AVISO do G2-03, o merge passou
>   e a silver está no `main`.
> - **`comms_gap`:** os 3 ativos sem comunicação durante 15 min, às 22:09. A hora das 22 teve 43/60 min (72%): BLOQUEIO
>   do G2-01, o merge foi recusado e a silver não está no `main` (404).
> - **No OpenMetadata:** os dois casos ficaram com histórico, no G2-03 e no G2-01. A linhagem bronze → silver_hourly →
>   silver está no catálogo.
> - **Falta:** o catálogo na Oracle (PLAN item 33, depois da revisão do código e do token no cofre) e o G3 (Fase 6).
> - **Nota:** a 1.ª tentativa do `comms_gap` não valeu: o Mac adormeceu, a VM local ficou em pausa e a hora falhou por
>   falta de dados. Testes locais longos precisam do Mac acordado e com a tampa aberta.
> **Estado:** contrato (`docs/gates-contract.md`) e ADR-008 feitos; G2 (7 regras) implementado e calibrado; hook Lua
> `pre-merge` + proteção `pre-commit` instalados no `industrial-lake`; 4 testes contra o lakeFS real e 34 locais a passar;
> primeira corrida real: o G2 recusou as horas com buracos de recolha. **Falta:** `comms_gap` injetado de propósito no
> simulador, OpenMetadata (catálogo, linhagem, owners, histórico dos gates), G3 (depende do gold, Fase 6).

OpenMetadata no cluster, conectores (PostgreSQL, datalake sobre SeaweedFS/lakeFS, linhagem do
Airflow), glossário de negócio, *owners* por dataset, test suites de G2/G3, e os *pre-merge
hooks* do lakeFS a impor o resultado.
**DoD:** injetar `comms_gap` faz G2 falhar por completude e **o merge é rejeitado**;
injetar `spike` gera `warn` e o merge passa; ambos ficam com histórico visível no
OpenMetadata; a linhagem no catálogo mostra o caminho OPC UA → bronze → silver → gold.

### Fase 6 — Gold + release tagging (Publish)  🟡 em curso (2026-11-22): ADR-019 decidido; Fase 6a (sinais novos) no disco, por rever

> **ADR-019 (escolhas do Stéphane, 22-11):** gold K3 (tudo do §5.4 com a tabela ao minuto), gold diário às 02:40 UTC com
> uma release por dia, `LATEST_STABLE` como ficheiro no `main` imposto por hook, G3 com 6 regras e 3 hooks, manifest
> com o commit de bronze de cada hora (a silver passa a registá-lo), OpenMetadata com propriedades por tabela.
> **Fase 6a, antes do gold:** fontes mais completas (potência da bomba e do motor, aspiração da bomba, caudal de água
> gelada, avarias e paragens, plano de produção) para os KPIs serem medidos e não estimados.
Agregados de negócio, dimensões, KPIs, mecanismo de release (tag lakeFS + manifest +
`LATEST_STABLE` + rollback), tags de release refletidas no OpenMetadata.
**DoD:** existem ≥3 releases; um rollback é executado e demonstrado em <1 min; a partir do
manifest consegue-se reconstruir que commit de bronze originou uma release.

### Fase 7 — BI + dashboard live
Sync gold→Postgres/Timescale a partir de `LATEST_STABLE`, Grafana operacional (latência
ponta-a-ponta <5 s), Superset com camada semântica sobre gold, dashboard executivo de KPIs,
dashboard de qualidade.
**DoD:** uma alteração no simulador aparece no Grafana em <5 s; o BI responde a perguntas
de negócio ("consumo específico da bomba por turno") sem SQL ad-hoc; a latência é **medida**,
não estimada.

### Fase 8 — ML de séries temporais
1. **Baselines primeiro** (naive, seasonal naive, média móvel) — sem baseline não há como
   afirmar que um modelo é bom.
2. **Deteção de anomalias** não supervisionada sobre vibração/temperatura.
3. **Previsão de falha / RUL** com o cenário `drift` como ground truth — a vantagem de
   simular é saber a verdade.
4. MLflow para tracking e registry; **cada run regista a tag lakeFS dos dados**; inferência
   batch via Airflow a escrever previsões para gold; backtest com validação temporal
   (nunca k-fold aleatório).
**DoD:** o modelo bate o baseline numa métrica declarada à partida; auditoria confirma
ausência de leakage nas features point-in-time; as previsões aparecem no dashboard; um
retrain a partir da tag registada reproduz as métricas.

### Fase 9 — Hardening e produto final
Prometheus/Loki, alerting, ~~CI~~ (feito: testes, Trivy, SBOM, imagens no GHCR), ~~Argo CD~~ (feito), ~~secrets~~ (feito:
OCI Vault + External Secrets Operator, ADR-011), backups (feito em parte: dump diário do Postgres para Object Storage e
backup semanal do disco, ADR-013; falta o versionamento do SeaweedFS e a garbage collection do lakeFS),
README com arquitetura e demo, bootstrap de raiz. Broker (decisão D9) se o gatilho se verificar.
**DoD:** destruir a VM e reconstruir tudo a partir do repo em <30 min, sem passos manuais
não documentados.

---

## 9. Orçamento de recursos (alvo: Oracle Always Free, 2 OCPU / 12 GB)

Desde 2026 o Ampere A1 gratuito da Oracle baixou de 4 OCPU / 24 GB para **2 OCPU / 12 GB / 200 GB de disco no total
da conta**. O desenvolvimento continua local (VM Lima, 8 CPU / 16 GB) e migra-se para a Oracle quando estabilizar
(Terraform em `terraform/oracle/` do repositório [platform](https://github.com/elwarrior100/platform)). Por isso o orçamento passa a ser o da Oracle.

**Medido em 2026-11-11** (VM local, núcleo + OpenMetadata a correr): ~6 GB de RAM usados, dos quais o OpenMetadata
~2,6 GB (OpenSearch 1,6 + servidor 1,0); dados em disco pequenos (SeaweedFS 83 MB, Postgres 357 MB; as imagens dos
contentores são o maior consumo, 16 GB). Uma hora de silver custa ~0,07 s de CPU e 183 MB de pico. Teste de carga
(`tests/stress/silver_backfill.py`, 1 160 horas, 11,9 M linhas, pod limitado a 2 CPU): 70 s com 2 workers, 137 s com 1;
pico do nó 1,9 de 8 CPU e 326 MB de RAM no processo.

Por isso a plataforma corre por **perfis**, não toda de uma vez, e **o trabalho corre em série**:

| Perfil | Componentes | RAM aprox. (medida/estimada) |
|---|---|---|
| `core` (F0-F4) | k3s, SeaweedFS, Postgres, lakeFS, Airflow, OT sim | ~4-5 GB (medido) |
| `core+gov` (F5-F6) | + OpenMetadata + OpenSearch | ~8-9 GB (medido ~6 GB + folga) |
| `core+bi` (F7) | + Grafana, Prometheus, Loki, Superset | a medir; ~+2-3 GB |
| `full` (F8-F9) | + MLflow (OpenMetadata pode ficar parado) | a medir |

**Execução em série (decisão de 2026-11-11):** com 2 CPUs, nada de paralelismo por omissão. `max_active_runs=1` nos DAGs
(a hora seguinte só começa quando a anterior acaba), um worker em backfills e ingestões, e as ingestões do
OpenMetadata fora da hora ao minuto 10, quando corre a silver. O custo é pequeno: 137 s para 1 180 horas com um só
worker, contra 70 s com dois, e uma hora normal demora ~1 s. A vantagem é um pico de CPU previsível e nenhum pod a
competir com o Postgres ou o lakeFS.

Folga recomendada na Oracle: swap de 4 GB e *heap* do OpenSearch reduzido (512 MB se for preciso).

**Regra:** um perfil é um conjunto de recursos do Argo CD, não um `kubectl apply` manual.

---

## 10. Riscos

| Risco | Mitigação |
|---|---|
| Âmbito enorme → desistência a meio | Cada fase entrega algo demonstrável; nunca duas fases abertas |
| OpenMetadata come a VM | Perfis (§9); heap do índice reduzido; parar governança durante trabalho de ML. Medido: ~2,6 GB, cabe em 12 GB |
| Oracle Always Free baixou para 2 OCPU / 12 GB (2026) e a Oracle pode recuperar VMs inativas ou excedidas | Orçamento do §9 já nesse limite; execução em série; **alerta de custos na consola OCI e confirmar o limite antes do `apply` (tarefa do Stéphane, §12.1)**; manter a hipótese Pay As You Go como plano B |
| VM Lima morta (Mac a dormir/app fechada) corrompe o fim dos ficheiros do SeaweedFS | `limactl stop industrial` antes de fechar; a rever `fsync`; já aconteceu (2026-11-09, ver ADR-008) |
| lakeFS: o padrão branch-por-batch acumula branches e lixo | Política de retenção e *garbage collection* definida logo na Fase 4, não na Fase 9 |
| Ler Parquet **através** do lakeFS tem fricção (DuckDB, Superset, MLflow) | Validar a leitura ponta-a-ponta com um teste mínimo **na Fase 0**, antes de comprometer a Fase 4 |
| Airflow em k8s consome tempo desproporcional | Se a Fase 4 passar de 2 semanas, correr Airflow fora do k8s temporariamente e voltar na Fase 9 |
| RS-232 virtual no macOS | O simulador vive na VM Linux — evita o problema de portas série no Darwin |
| ML sem sinal (dados demasiado limpos) | Cenários de degradação desenhados na Fase 1, antes de haver pipeline |
| Dashboard "live" que afinal é de 30 s | Latência ponta-a-ponta é métrica de primeira classe desde a Fase 3 |

---

## 11. Perguntas em aberto

1. **"E afins"** — que mais usou o Gabriel? Confirmados até agora: PostgreSQL, lakeFS,
   OpenMetadata, Airflow. Por confirmar: houve broker? dbt? Trino? Ferramenta de qualidade
   à parte do OpenMetadata? A resposta pode mexer nos ADRs 006, 009 e 010.
2. Ele usou **hooks de pré-merge** do lakeFS ou o lakeFS só como storage versionado? Se foi
   só storage, o desenho da §2 é uma melhoria em relação à referência — vale a pena dizê-lo
   no README.
3. Que conectores de OpenMetadata estavam configurados (linhagem de Airflow? profiler ativo?).
4. Existe equipamento Modbus físico a que ligar mais tarde, ou fica tudo simulado?
5. O "produto fixe no final" é o dashboard, ou uma aplicação própria sobre uma API do gold?

---

## 12. Estrutura de repositório proposta

```
modbus_data_plataform/
├── CLAUDE.md                 regras de colaboração
├── README.md                 arquitetura + como arrancar (Fase 9)
├── docs/
│   ├── PLAN.md               este ficheiro
│   ├── adr/                  decisões (001-…)
│   ├── register-map.md       mapa Modbus (Fase 1)
│   ├── data-contracts.md     contratos bronze/silver/gold
│   └── runbook.md            operação e troubleshooting
├── ot/                       simulador + gateway OPC UA
├── platform/                 k3s bootstrap, SeaweedFS, lakeFS, Postgres, OpenMetadata, Argo CD
├── ingest/                   collector, spool e writer do bronze (Fase 3)
├── pipelines/                DAGs Airflow + transformações (Fase 4)
├── quality/                  test suites + hooks lakeFS
├── governance/               config de conectores e glossário OpenMetadata
├── ml/                       features, treino, backtest
├── serving/                  dashboards, camada semântica
└── tests/
```

### 12.1 Separação em plataforma + projetos (ADR-010, proposto em 2026-11-12)

A estrutura acima é a de **hoje** (um repositório). O alvo é dividir em dois tipos (👉 `docs/img-26-plataforma-e-projetos.svg`):

- **`gitops` (plataforma, genérico):** `terraform/oracle/`, `platform/` (Airflow, Postgres, lakeFS, OpenMetadata,
  Argo CD, partilhados por todos), `projects/<nome>/` (um projeto = uma pasta, lida por um ApplicationSet do Argo CD)
  e `envs/local` · `envs/oracle` (ajustes por ambiente).
- **Um repositório por projeto** (este é o primeiro): código, DAGs, Dockerfiles, testes, docs, e CI que publica
  imagens arm64 num registo (GHCR) com tag = SHA do commit.

Fluxo: push no projeto → CI → imagem no registo → commit da tag no `gitops` → o Argo CD de cada cluster aplica.
Os DAGs saem da imagem do Airflow (git-sync por projeto) e cada tarefa corre com a imagem do seu projeto. Na Oracle,
cada projeto tem namespace com ResourceQuota e um pool do Airflow partilhado mantém tudo em série.

**Decisões de 2026-11-14:** repositório `elwarrior100/platform` **público** (para o CV; o do projeto fica como está);
segredos num **cofre (OCI Vault) lidos pelo External Secrets Operator** desde já (ADR-011), não em scripts nem no git.

**Ordem de trabalho:**
1. ✅ Alerta de custos na consola OCI (feito pelo Stéphane, 2026-11-13).
2. ✅ Trabalho das Fases 4-5 commitado.
3. ✅ (2026-11-14) `platform/` separado para [elwarrior100/platform](https://github.com/elwarrior100/platform)
   (público), com a história, reorganizado em `platform/`, `projects/modbus/`, `envs/`, `terraform/`; `gitleaks` limpo.
   O Argo CD passou a ler esse repositório (antes lia um `main` antigo deste, e o `ingest` não era gerido por ele);
   `ot` e `ingest` vêm de um ApplicationSet. `platform/` foi apagado deste repositório; os scripts estão em
   `../platform/scripts/` (o repositório `platform` clonado ao lado deste).
4. ✅ (2026-11-14) Cofre: Terraform criou compartment, OCI Vault, chave e `eso-reader` (e importou a VM `agente-01`
   sem a recriar); `vault-seed.sh` copiou 11 segredos; o ESO lê-os e os 13 Secrets do cluster passaram a vir do
   cofre com os mesmos valores (hash igual, nenhum pod reiniciado). Os scripts já não geram palavras-passe.
5. ✅ (2026-11-14) CI (`.github/workflows/ci.yml`): testes em arm64 a cada push; no `main` publica `modbus-ingest`,
   `modbus-ot-edge` e `modbus-pipelines` no GHCR (públicos), tag = SHA do commit. Os dois clusters correm essas imagens.
6. ✅ (2026-11-14) Airflow com a imagem oficial; os DAGs vêm deste repositório por DAG bundle (fixos no commit) e cada
   tarefa corre com `modbus-pipelines:<sha>` (`pod_override`); `parallelism = 2`, `max_active_runs = 1`. Provado no local.
8. 🔵 (2026-11-14) Plataforma na Oracle, de raiz (sem migração): k3s, Argo CD, cofre por instance principal, Postgres,
   SeaweedFS, lakeFS com `industrial-lake` e os hooks do G2, OpenMetadata, Airflow, recolha a correr desde 14:13 UTC.
   **Falta a prova:** 24 h seguidas de recolha sem buracos (G2 `pass`) e uma hora com o hook a funcionar lá.
9. ✅ (2026-11-14) Revisão crítica independente (44 → 56 → 66,75/100) e correções: backups com restauro testado,
   encerramento gracioso, admissão, NetworkPolicies, monitorização (Grafana) e alarmes da OCI, Airflow 3.3.2 (ADR-013).
7. ✅ (2026-11-14) Rede privada (Tailscale) entre o Mac e a VM da Oracle (`platform-oracle`); a porta 22 pública foi
   fechada por Terraform (`network.tf`): a VM não tem nenhuma porta aberta à internet, entra-se com `ssh ubuntu@platform-oracle`.
10. ✅ (2026-11-14) 4.ª e 5.ª revisões (74 → 77,75): Ingress pelo Traefik (`http://<svc>.localhost` e
   `http://<svc>.100.108.9.15.sslip.io`, só pela tailnet); depois chave do SeaweedFS só de leitura para a cópia do lake,
   projeto `bootstrap` para a `root` e `default` vazio, saída do backup/ops só para as faixas OSN da Oracle, ensaio de
   restauro do lake (`lake-restore-test.sh`: 374/374 objetos).
11. ✅ (2026-11-14) 6.ª revisão crítica: 79,6/100. Corrigido logo o arranque de um cluster novo (o projeto `bootstrap`
   tem de existir antes da `root`); ensaio de restauro com imagem fixa e prazo.
13. ✅ (2026-11-14) Pós-6.ª revisão (ADR-014): ensaio de restauro do lake **diário** (`ops/lake-restore-drill`, MD5 de
   cada objeto, alarme `lake_drill_success` em falta há 2 dias), pods recusados em namespaces sem
   `platform/network-reviewed`, `trivy config` no CI, lakeFS fixo por digest, cópias locais do estado do Terraform apagadas.
14. ⬜ Silver local a falhar desde a hora 18 (G2: 65% dos minutos e 158 leituras em conflito); a Oracle está bem. Causa
   provável (7.ª revisão): a VM do Lima em pausa quando o Mac dorme (buracos de 15–29 min no writer). Confirmar.
16. ⬜ **Crescimento do lake** (medido 2026-11-14: 33 MB em 9,5 h na Oracle, ~80 MB/dia, 30% metadados do lakeFS por
   haver um commit por minuto). A este ritmo: alarme dos 12 GB em ~5 meses, 20 GB grátis em ~7–8. Plano (ADR-015):
   commits da bronze menos frequentes, compactação horária, retenção da bronze crua, GC do lakeFS (Spark, semanal) e
   poda da cópia imutável com uma identidade que não a da VM.
   - ✅ (2026-11-14) **Retenção da bronze crua: 15 dias** (decisão do Stéphane), aplicada pelo `lake_housekeeping`
     (03:30) nos dois clusters; provada (0 dias a sair, como devia). Primeira remoção real na Oracle a 2026-11-30.
   - ✅ (2026-11-15) GC do lakeFS (semanal, Oracle, só a marcar) e poda mensal da cópia (`lake-backup-prune.sh`).
   - ⬜ **Ler o relatório da GC de 2026-11-22 e ligar a remoção antes de 2026-11-30** (decisão do Stéphane). A
     primeira remoção deve apagar o relatório lido (`do_mark=false`, `mark_id=<id>`), não marcar outra vez.
17. ✅ (2026-11-15) 8.ª revisão (83,9/100), tratado (resta o disco só de leitura no backup/ops): reservar CPU/memória ao sistema no kubelet e limites de CPU nos pesados
   (causa do incidente do k3s); a validade das exceções do Trivy só dispara com um scan agendado; folga de memória
   da GC (PriorityClass baixa, driver menor); sinal de vida da GC; o teste de arranque também para a Oracle e mais
   fundo; imagem da GC por digest e sem token de service account; invariante GC (15 d) ≥ vida dos dumps (14 d);
   disco só de leitura no backup/ops. ✅ Corrigido logo: a poda só apaga o que está em falta há 30 dias.
   - ✅ DAGs novos começam ativos (`dags_are_paused_at_creation: False`): o git decide o que corre.
15. ✅ (2026-11-15) 7.ª revisão (81,6/100) corrigida: o ensaio de restauro não escala
   (um `stat` por objeto, duas cópias do lake no disco), multipart sem verificação, pods de backup/ops sem
   `securityContext`, disco real (47 GB) diferente do Terraform (100 GB), o ensaio não deixa rasto.
   Feito: ensaio que escala (1668 objetos em segundos, alarme imediato se falhar), pods endurecidos (CI falha em
   HIGH), disco a 100 GB (o limite grátis é 200), imagens dos charts por digest, teste de arranque do zero no CI.
   Incidente: aplicar os digests de uma vez levou a carga a 14 e o k3s reiniciou (sem perda); runbook: aos poucos.
12. ⬜ **A seguir à 6.ª revisão (não esquecer):** operador Kubernetes do Tailscale, para nomes com HTTPS grátis
   (`https://airflow.<tailnet>.ts.net`, grafana, argocd, lakefs, openmetadata). Precisa de um OAuth client do Tailscale
   criado pelo Stéphane (segredo no cofre); ~30–50 MB de RAM por serviço. O Ingress por `sslip.io` fica como alternativa.

---

## 13. Próximo passo

A Fase 0 **não** começa pelo k3s. Começa por matar a incerteza mais cara.

### Tarefa 1 — `git init` (10 min)
Repo, estrutura de pastas da §12, commit inicial com `CLAUDE.md` e `docs/PLAN.md`.
Ritual barato que dá casa a tudo o resto.

### Tarefa 2 — Spike lakeFS ↔ DuckDB (timebox: 2 h, fora do k3s)
**Porquê primeiro:** todo o desenho das Fases 4-8 assume que se lê Parquet a partir de um
branch lakeFS com o DuckDB, através do gateway S3. Se isso tiver fricção séria, o plano muda.
É a suposição com maior custo de estar errada e das mais baratas de testar — logo, é a
primeira. Fazer isto **em Docker no Mac**, não no k3s: o objetivo é responder à pergunta em
duas horas, não construir infraestrutura.

Passos e perguntas a responder:

1. lakeFS + SeaweedFS a correr localmente (quickstart oficial do lakeFS); repositório `spike`.
2. Criar branch `test` a partir de `main`.
3. Escrever ~100k linhas de Parquet particionado por dia para `test`, sob `bronze/dt=…/`.
4. **Isolamento:** o ficheiro aparece em `test` e **não** aparece em `main`? (Se falhar aqui,
   pára tudo — não percebeste o modelo do lakeFS.)
5. **Leitura:** o DuckDB lê via gateway S3, com o branch no caminho? Faz *glob* de partições
   (`**/*.parquet`)? Precisa de credenciais próprias do lakeFS ou reutiliza as do SeaweedFS?
6. **Performance:** ler as 100k linhas demora quanto, comparado com ler direto do SeaweedFS?
   Ordem de grandeza chega — só interessa se for 10× pior.
7. Commit no branch, merge para `main`, confirmar que agora aparece em `main`.
8. Criar uma tag e ler a partir dela (é assim que o ML e o BI vão consumir).
9. **Ler duas refs em simultâneo** (branch vs `main`) na mesma query. Isto é o que torna os
   gates de reconciliação do G3 possíveis — se não der, o G3 precisa de outro desenho.

**Entregável:** `docs/adr/000-spike-lakefs-duckdb.md` — o que funcionou, o que doeu, tempos
medidos, e a decisão: seguir com o desenho da §2, ou ajustá-lo. Curto: uma página chega.

**Critério de sucesso:** os 9 pontos respondidos com evidência. Não é "correu bem" — é
"o passo 9 funciona/não funciona, e aqui está o output".

### Tarefa 3 — ADR-001 (runtime k8s)
Defende k3s em VM Lima contra k3d em Docker, em duas frases. Só depois se instala fosse o
que for de k3s.

---

### O que eu faço enquanto fazes isto
*(Texto histórico da Fase 0, de quando a regra era o Stéphane escrever o código. Desde 2026-10-24 o Claude escreve o
código e explica-o; ver CLAUDE.md.)*
18. ✅ (2026-11-15) HTTPS `https://<serviço>.taila669aa.ts.net` por Tailscale Services na VM (sem operador: proxies
   privilegiados); proteção de ramos no GitHub (`main`, `oracle`); CPU/memória reservadas ao k3s (causa do incidente);
   OpenMetadata e OpenSearch emagrecidos (~800 MB livres); teste de arranque do zero nos dois ambientes (apanhou os
   projetos do Argo CD criados depois das aplicações: corrigido com sync-wave -1).
19. ⬜ Ideia: `scripts/new-project.sh <nome>` (namespace, rede, AppProject, segredos, pasta de manifests) e um token
   do GitHub para o CI do projeto atualizar sozinho a imagem/DAG bundle na plataforma.
20. ✅ (2026-11-15) 9.ª revisão: **86,25/100**. Corrigido logo: despejo por memória no kubelet (k3s `config.yaml`) e
   comentários falsos sobre prioridade; `promote-oracle.sh` só aceita commits do `main`; sinal de vida da GC só conta
   execuções do CronJob. Por fazer (9.ª): teste de arranque com as aplicações filhas no commit em teste; sweep com
   lista visível e marca recente (< 48 h); regra de saída para a API só no pod gc-freshness e IP privado fixo no
   Terraform; política do Tailscale no git; re-scan semanal de todas as imagens; disco só de leitura no backup/ops.
21. ✅ (2026-11-15) **Custo zero reposto**: a cópia do lake e o ensaio passavam os 50 mil pedidos grátis do Object
   Storage (88 mil em 2 dias, €0,012). Agora em pacotes (ADR-016): ~4 pedidos por noite; ensaio a partir dos pacotes
   (1890/1890 MD5); alarme se um dia passar de 1 500 pedidos. Também: disco só de leitura no backup/ops, teste de
   arranque da árvore toda no commit, poda/GC com lista à vista, scan semanal de todas as imagens (apanhou o modelo
   das tarefas do Airflow em 3.2.2), política do Tailscale no git, `new-project.sh`.
   - ✅ (2026-11-16) Cópia antiga objeto a objeto apagada (12 913 objetos) a pedido do Stéphane; bucket só com
     bundles/ e catalog/; regra de ciclo de vida temporária retirada.
22. ✅ (2026-11-15) 10.ª revisão: **88,4/100**. Corrigido logo: sinal de vida da GC ignora corridas manuais (com período
   de graça), lake-sync falha num erro de listagem e repõe o completo se o dia 1 falhar, `pipefail`, poda guiada pelos
   catálogos, envios a meio abortados em 2 dias, despejo por memória com 60 s, orçamento que avisa ao primeiro cêntimo.
23. ✅ (2026-11-16) Auditoria de custos (conta PAYG): retenção da spool (7 dias exportados / 30 rejeitados), cópia do
   lake com completo semanal e remoção automática aos 9 dias, alarme do total de Object Storage > 15 GB, ABORT de
   multipart nos dumps, alarmes de "sinal em falta" corrigidos ([1d]/[3h].absent()).
   - ✅ ADR-016 atualizado (completo semanal + remoção aos 9 dias).
   - ⬜ Ligar a remoção da GC após o relatório de 22-11; atualizar o livro com as lições da auditoria.
24. ✅ (2026-11-16) **Corta-custos automático (ADR-017)**: ops/cost-guard de hora a hora; a €1,00 no mês põe quotas
   a zero (48 famílias) e faz SOFTSTOP à VM, nunca apaga dados. Retoma com `scripts/cost-guard-resume.sh`. Testado:
   dry-run na VM, regras de quota aceites (compartimento de teste, apagado), corrida real a €0,0198. Substitui as
   "quotas preventivas" do item 23: as quotas só entram quando o limite é atingido.
   - ✅ Teste real a 2026-11-16 (07:21): quota criada, VM STOPPED em ~1,5 min, retoma com o script, 19 apps
     Healthy em ~10 min. O script já não etiqueta `cost-guard-ack` se o gasto estiver abaixo de €1.
25. ✅ (2026-11-16) **Memória aparada sem perder funções** (aprovado pelo Stéphane, só o que não baixa o score):
   - **Prometheus:** deixou de guardar 8 histogramas internos do k3s que ninguém usa; ficam o SLI do API server e
     `workqueue_queue_duration` (painel do dashboard do API server). Séries do kubelet ~51k → ~26k, do apiserver
     ~35k → ~19k. Memória 700 → 493 Mi (desce mais quando a "head" compactar).
   - **Grafana:** datasources carregados por um init container (modo LIST) e alertas do Grafana desligados.
     Memória 685 → 325 Mi.
   - **Airflow:** o processador de DAGs passou a usar 1 processo (273 → 255 Mi); as 3 DAGs carregam sem erros.
   - **Ficou de fora de propósito:** Argo CD (já afinado), baixar os requests (desprotegia o Postgres) e desligar o
     OpenMetadata.
   - ✅ 11.ª revisão de production readiness: **89,0/100** (+0,6). Subiu a segurança de custos; desceu pelo CI
     vermelho (o check GC ≥ dumps lia também a regra ABORT nova) e pelo runbook desatualizado.
26. ✅ (2026-11-16) Correções da 11.ª revisão (todas grátis):
   - **CI** de novo verde; o `promote-oracle.sh` recusa promover um commit sem CI verde (testado: recusou com o CI a
     correr e promoveu depois de ficar verde).
   - **Alertas do Prometheus chegam ao email:** o `node-health` publica `prometheus_alerts_firing` e há um alarme
     na Oracle acima de 0 (hoje: 0).
   - **Runbook:** o restauro do lake por pacotes, a limpeza automática aos 9 dias e as tabelas de alarmes e
     salvaguardas atualizadas.
   - **Corta-custos:** lê qualquer número, a métrica já não o pode parar, e um mês retomado tem teto (gasto na
     retoma + €1).
   - **Reconciliação** (`tests/phase3/reconcile.py`): "a mais" conta só a janela que o spool guarda. Local: OK,
     0/0/0, com 1 512 808 eventos na janela.
   - ✅ 12.ª revisão: **90,2/100** (+1,2). Os 5 pontos foram verificados.
27. ✅ (2026-11-16) Performance (agente de performance; tudo grátis, nada desligado):
   - **Verificação de saúde do Airflow** passou de cada 60 s para cada 300 s. Cada verificação gastava ~3,4 s de CPU,
     ~22% da CPU de todos os contentores.
   - **Postgres:** `wal_compression=lz4`, `checkpoint_timeout=15min` e `max_wal_size=2GB`. Escrevia ~16 GB de WAL em
     47 h, 95% em páginas completas.
   - **DAGs relidas de 300 em 300 s** e o bundle de 600 em 600 s (o código está fixado num commit).
   - **Correções da 12.ª revisão:** o corta-custos ignora um gasto calculado antes do mês atual (viragem do mês); o
     runbook diz para religar a VM em menos de 9 dias; o texto do alarme de pedidos diz ~6 por noite.
   - **Custo de novembro:** €0,0198 a 16-11 (budget calculado às 09:03). A limpeza manual da cópia antiga gastou
     ~20 mil pedidos e passou o limite grátis do mês; o alarme de pedidos apaga-se quando a janela de 1 dia passar.
     Lição: apagar muitos objetos faz-se com uma regra de ciclo de vida (grátis), não objeto a objeto.
   - ⬜ Correr `tests/phase3/run.sh` contra a Oracle (a ferramenta de segurança bloqueou o acesso aos segredos de
     produção; corre o Stéphane, ou dá permissão).
   - ✅ (21-11) Medido: scheduler do Airflow 0,079 → 0,038 núcleos, processador de DAGs 0,082 → 0,022 (~0,1 núcleo
     poupado). WAL do Postgres ~347 → ~286 MB/h (−18%, abaixo dos 2–2,5× estimados: as páginas completas vêm sobretudo
     do índice UUID aleatório e do insert + update da spool; o remédio de fundo é mudar o desenho da spool, não
     urgente com o disco a 1–2% de utilização).
28. ✅ (2026-11-16) RAM, agente de memória, pontos 1 a 6 (aprovados: não baixam o score nem limitam projetos futuros):
   - **k3s:** `GOMEMLIMIT=900MiB` (547 MB vivos; o GC só limpava a 1,12 GB). RSS 1,48 → 1,04 GB.
   - **lakeFS:** cache de sstables em memória 100 MB (era 400) e 5 ligações paradas (eram 25). 331 → 75 Mi, acabado
     de arrancar.
   - **Argo CD:** controller com `GOMEMLIMIT=512MiB`. 391 → 313 Mi.
   - **Grafana:** 13 datasources desligados. Processos de plugins 14 → 1.
   - **OpenMetadata:** pool de ligações com máximo 20 e 5 paradas. Antes, o máximo de 100 podia esgotar as 100
     ligações do Postgres. Ligações ~30 → 15.
   - **VM:** fwupd, ModemManager, udisks2 e multipathd mascarados.
   - **Resultado na VM:** usada 7,26 → 6,54 GB; disponível 4,66 → 5,38 GB.
   - ⬜ Ficaram por fazer: plugins do OpenSearch (risco médio; testar a pesquisa do OpenMetadata) e plugins do Oracle
     Cloud Agent na consola (Custom Logs, Run Command), a fazer pelo Stéphane.
29. ✅ (2026-11-17) **Incidente da noite e auditorias de "bombas-relógio" e de código/segurança** (tudo grátis):
   - **Incidente:**
     - a cópia do lake e o backup do Postgres passam a enviar num só pedido (multipart precisa de `OBJECT_OVERWRITE`,
       que a VM não tem de propósito);
     - o ensaio tem SeaweedFS com volumes maiores, 1 GiB de memória, índice em disco e prazo de 1 h;
     - o lakeFS ficou com `range_raggedness_entries=1000`: os índices crescem ~67 MB/dia (eram ~600);
     - as reservas de CPU baixaram de 97% para 79%.
   - **Bombas-relógio:** GC com prazo de 3 h; disco temporário da cópia 20 Gi; dumps guardados 7 dias (eram 14).
   - **Segurança:**
     - o Grafana já não lê segredos (ClusterRole próprio, só configmaps);
     - as tarefas do Airflow ficam sem token do Kubernetes;
     - a chave do lakeFS chega só às tarefas, e não ao scheduler, à API nem ao processador de DAGs;
     - a regra de admissão verifica também os contentores de depuração e as capacidades extra, e só abre hostPath ao
       node-exporter.
   - **Dados:**
     - alarme "silver hours missing or late" (horas sem silver ou bronze chegado depois);
     - a limpeza da spool guarda sempre o evento mais recente de cada sensor (evita duplicados ao fim de 7 dias).
   - **Runbook:** o procedimento "lakeFS ilegível" foi corrigido e há um novo, "perdi a VM inteira".
   - ⬜ **Ainda por fazer:**
     - ✅ (21-11) pacote completo partido em partes de ≤3 GiB de objetos (ADR-016, revisão de 21-11), testado na Oracle;
     - medir a GC de 22-11 (tempo e memória com mais commits);
     - regras do Tailscale na consola (Stéphane; texto dado na conversa de 17-11).
34. 🟡 (2026-11-22) **A primeira GC semanal agendada falhou** às 05:00: o lakeFS demorou a listar o que não estava
   commitado em **4 464 branches** (um `etl-*` por lote, guardados 7 dias), e o cliente da GC desistiu pelo tempo de
   espera por omissão ("postgres scan: context canceled"). Uma bomba-relógio que a auditoria não apanhou.
   - **Plataforma (feito e promovido):** a GC ganhou tempos de espera do cliente: ligação 60 s, leitura 30 min.
   - **Projeto (no disco, por rever):** o writer apaga o branch do lote logo a seguir ao merge (os recusados ficam).
     Testes contra um Postgres real, incluindo matar o writer em cada passo: 29 passam.
   - **O pacote completo semanal (03:00) e o ensaio (04:15) correram bem:** 33 389 objetos, 904 MB, 1 parte.
   - ✅ A GC manual (só marcar, 11 min) correu bem com os tempos novos: marcou **2 objetos** órfãos. Só 2 porque o lake da
     Oracle só existe desde ~14-11, e o apagado fica 15 dias: a GC liberta espaço a sério a partir de dezembro.
   - ✅ **Remoção revista** (Stéphane: "sim"): o Job apagou exatamente essa marcação em 37 s, e confirmou-se no SeaweedFS
     que os 2 objetos desapareceram. O `lakefs-gc-sweep.sh` encravou no passo de pré-visualização (o port-forward morreu
     pelo relay do Tailscale); como a lista já tinha sido revista, correu-se só o passo final, igual ao do script.
   - ✅ **A GC semanal passa a marcar E apagar** (`GC_SWEEP=true`), promovida para a Oracle.
   - ✅ **Código revisto em produção** (commit 82f3868 do projeto, aceite pelo Stéphane): writer de 5 em 5 min, silver
     às :15, o branch do lote apagado depois do merge, OpenMetadata. O silver das 13:15 na Oracle deu "pass" limpo (sem
     o aviso falso do G2-02) e enviou o catálogo e o G2 para o OpenMetadata da Oracle.
33. 🟡 (2026-11-21) **OpenMetadata: catálogo, linhagem e histórico do G2** (Fase 5; ADR-009, decisão 5). Código do
   projeto **no disco, sem commit, por rever**:
   - `pipelines/src/silver/openmetadata.py`: cria e mantém o serviço `lakefs` → `industrial_lake` → `bronze.events` e
     `silver.readings`, com as colunas dos contratos. Também a linhagem bronze → (`silver_hourly`) → silver e os 7
     casos de teste G2-01…G2-07. Envia um resultado por regra e por hora (passa → Success; aviso ou bloqueio → Failed,
     com o texto a dizer qual). É sempre "melhor esforço": se o OpenMetadata estiver em baixo, o silver não falha.
   - `run.py` chama-o depois do G2; `tests/test_openmetadata.py` tem 3 testes.
   - **Testado no OpenMetadata local:** o catálogo, a linhagem, os 7 testes e um resultado de exemplo (já apagado).
   - **Histórico real no OpenMetadata local** (21-11): 36 atestações do lakeFS local enviadas com
     `python -m silver.openmetadata` (31 horas aprovadas, 5 recusadas). **Achado:** o G2-02 deu aviso em TODAS as 36
     horas (leituras do mesmo sensor no mesmo instante com valores diferentes). **Causa encontrada (21-11):** o
     `source_ts` tem precisão de 1 segundo, e o simulador atualiza ~1 vez por segundo. Duas leituras verdadeiras,
     ~1 s uma da outra, ficam com o mesmo `source_ts`; o `server_ts` (ms) mostra que são diferentes. Exemplo:
     PUMP-01.flow_rate 219,4 e 224,2 em `09:02:24`, `server_ts` `.839` e `25.838`. 196 de 43 810 linhas (0,45%), em
     sinais rápidos (vibração, potência, caudal). Não são dados errados. **Decidido: solução intermédia (ADR-018).** O `source_ts`
     fica honesto, o G2-02 desempata pelo `server_ts`, a silver ordena-se pelo `server_ts` dentro do mesmo segundo
     (`silver_version` "2"), e os contratos dizem a precisão. Código por rever; em 11 horas reais, 2 542 → 0 falsos
     conflitos, e um conflito verdadeiro continua a ser apanhado (teste).
   - **Plataforma preparada:** `scripts/openmetadata-bot-token.sh`, os ExternalSecrets e as variáveis nas tarefas do
     Airflow, já a funcionar no local. Na Oracle falta o token no cofre: a ferramenta de segurança não me deixou criá-lo;
     corre `KUBECONFIG=~/.kube/oracle.yaml ./scripts/openmetadata-bot-token.sh oracle` e eu promovo a seguir.
   - ⬜ **Para chegar à Oracle, depois de reveres e fazeres commit:**
     1. guardar no cofre o token do bot de ingestão (`airflow-openmetadata-bot`, prefixo permitido no namespace
        airflow);
     2. criar um ExternalSecret no airflow, `OPENMETADATA_URL` e `OPENMETADATA_TOKEN` no `workers.env` e uma regra de
        entrada no namespace openmetadata para as tarefas do Airflow;
     3. imagem nova do pipelines e `tracking_ref`.
32. ✅ (2026-11-21) **Reparação real do lakeFS local** com o runbook "lakeFS main is unreadable": o `main` perdeu o
   índice do último commit (a VM local foi desligada sem `vm-stop.sh`). Foi feito hard reset para o último commit
   legível, e o único lote perdido voltou à fila e foi reescrito. O runbook ganhou três lições:
   - o Argo CD desfaz o "parar o writer";
   - a proteção pede `If-Match`;
   - o branch `etl-*` do lote perdido não se apaga (410): faz-se hard reset dele para o `main`, senão um lote encravado
     bloqueia todos os seguintes.
31. ✅ (2026-11-21) **Custo escondido nas leituras dos alarmes** (encontrado por um agente auditor de custos):
   - **O problema:** os 4 alarmes `[1d].absent()` (backup, cópia, ensaio, verificação da GC) liam ~110 milhões de
     pontos por dia. O grátis é 1 000 milhões por mês; acabaria a ~25-11, com novembro a ~€0,6–0,8 e ~€3/mês daí em
     diante. O corta-custos não ajudava: os alarmes correm na Oracle, não na VM.
   - **A correção:** `ops/job-freshness` envia de 30 em 30 min as horas desde o último sucesso de cada CronJob
     (`lastSuccessfulTime`); os alarmes passam a "mais de 26 h" numa janela de 1 h (a GC, "mais de 194 h"); e há um
     alarme `[3h].absent()` para o próprio verificador.
   - **Testado:** um alarme temporário (GC > 100 h, emails suprimidos) passou a FIRING em ~4 min e foi apagado; todos os
     alarmes reais estão OK.
   - ⬜ Confirmar a 22/23-11 nos dados de uso que as leituras caíram para poucos milhões por dia.
   - **Regra nova:** nunca janelas `[1d]` em alarmes da Oracle.
30. ▶️ (2026-11-17 → 2026-11-21) **Projeto modbus esteve em PAUSA na Oracle; retomado a 21-11** a pedido do
   Stéphane, com o MESMO código de antes. As mudanças por rever abaixo continuam por aplicar (passos 1 e 2 da retoma).
   Retoma feita: patch removido, `modbus_running = true`, deployments a 1 pelo Argo CD, DAGs ativas (o catchup percorre
   as horas da pausa, sem bronze; a hora 10 de 17-11 ficou parcial e o G2 recusa-a, como deve).
   - **Parado:** ot-edge, collector e writer estão a 0, por esta ordem, depois de a spool ficar vazia (0 por atribuir,
     0 pendentes). As DAGs `silver_hourly` e `lake_housekeeping` estão em pausa. O Argo CD da Oracle não sincroniza os
     projetos (patch em `envs/oracle/apps/kustomization.yaml`). Os alarmes do silver estão desligados
     (`modbus_running = false`).
   - **Continua a correr:** a plataforma, os backups, a cópia do lake, o ensaio, a GC, o corta-custos e os alarmes. O
     lake fica congelado (~1 GB) e a spool encolhe até 1 evento por sensor ao fim de 7 dias. Custo: €0.
   - **Por rever, no projeto (no disco, sem commit; testes a passar):**
     - `pipelines/src/silver/run.py`: lê o bronze do `target`, e não do branch, para repetir uma hora falhada
       funcionar mesmo que tenha chegado bronze depois;
     - `ingest/src/ingest/collector.py`: a fila em memória tem teto (~3 h); acima disso descarta as amostras novas,
       com aviso, em vez de morrer por falta de memória;
     - `pipelines/dags/silver_hourly.py`: o silver passa a correr à hora e 15, para ir junto com o writer a 5 min;
     - `tests/phase3/reconcile.py`: só compara a janela que a spool guarda (de 16-11).
   - **Para retomar, por esta ordem:**
     1. rever e fazer commit do código acima; CI das imagens; atualizar `MODBUS_PIPELINES_IMAGE`/`tracking_ref` e a
        imagem do ingest na plataforma;
     2. pôr `BATCH_AGE_S=300` em `projects/modbus/k8s/ingest/ingest.yaml`, só JUNTO com a DAG às :15;
     3. apagar a linha `remove .../automated` do kustomization da Oracle e pôr `modbus_running = true`, depois
        `terraform apply`;
     4. pôr o writer, o collector e o ot-edge a 1 (o Argo CD repõe-nos) e tirar as DAGs de pausa;
     5. em 1 h, o alarme das horas de silver deve ficar em 0.
35. 🟡 (2026-11-22) **Fase 6a: fontes mais completas** (ADR-019, D11). Código **no disco, sem commit, por rever**:
   - **Simulador:** `PUMP-01.power_kw` e `MOTOR-01.power_kw` (derivadas dos outros sinais), `PUMP-01.inlet_pressure`,
     `CHILLER-01.chw_flow`; ativo parado com valores de repouso (o processo a 0, as temperaturas a arrefecer); cenários
     `trip`, `planned_stop` e `maintenance`; avarias aleatórias (`--trip-rate-per-day`); plano semanal em hora de Lisboa
     (`plan.yaml`, `--plan builtin`). `run_hours` só conta a funcionar. **Mudança de comportamento:** o `drift` também
     para a máquina quando chega à falha (antes, em FAULT a bomba continuava a vibrar).
   - **Contratos e espelhos:** `register-map.md` (sinais, 7.1 ativo parado, cenários, plano), `devices.yaml`,
     `opcua-address-space.md`, `nodes.yaml` (G1), `catalog.yaml` (+ `moves_with_run`), `gates-contract.md`.
   - **G2-06:** um ativo parado a hora toda já não dá aviso de "sensor parado".
   - **Testes:** simulador 56, gateway 23, pipelines 48 (+14 com lakeFS), ingest 15 (+21 com Postgres), todos a passar.
     DoD das Fases 1 e 2 para os sinais novos: um cliente Modbus independente lê-os em TCP e RS-232, um cliente OPC UA
     vê-os GOOD, com unidades e `EURange`.
   - ⬜ **Depois de reveres e fazeres commit:** imagens no CI; na plataforma, `PLAN=builtin` e `TRIP_RATE_PER_DAY=1` no
     `ot-edge` (primeiro local, depois a Oracle); medir o volume real (+~20% previsto) e uma hora de silver com G2 limpo.
   - ⬜ **Depois:** o gold (ADR-019): T1 na silver, gold K3, G3 + hooks, releases + rollback, OpenMetadata.
   - 🟡 (2026-11-23) **No disco, por rever, sem commit** (ADR-019 secção 8): T1 na silver; gold K3 (`pipelines/src/gold/`,
     9 tabelas por dia); G3 com 6 regras e a atestação; hooks G3 e das tags escritos mas **não instalados**; `run_day`
     e o DAG `gold_daily`. Medido: ~2 s e ~420 MB de pico por dia (1,2 M amostras). ⬜ releases + `LATEST_STABLE` +
     rollback; ⬜ OpenMetadata (M1); ⬜ provar os hooks num repositório descartável; ⬜ retenção da silver (3 meses).
36. 🟡 (2026-11-22) **Crescimento do lake e metadados do lakeFS** (ADR-020). Medido: 80% do lake são metadados do lakeFS
   (1,02 GB de 1,31 GB na Oracle), que nunca se apagam e crescem com os commits; a silver cresce ~36 MB/dia e também não se
   apaga. Sem mudanças, os 20 GB grátis do Object Storage acabariam em meados de 2027.
   - **Fase 6a implantada no cluster local** (plataforma `1e5edd6` + `912b886`): 52 578 amostras/h (+18,6%), 0 lotes
     recusados pelo G1, memória igual.
   - **No disco, por rever:** writer com lotes de 15 min (`BATCH_AGE_S=900`, `BATCH_ROWS=20000`, plataforma) e a silver
     às :25 (projeto). 3× menos commits. Testes todos verdes, incluindo contra o PostgreSQL e o lakeFS reais do cluster
     local (ingest 36, pipelines 62).
   - ✅ **Retenção da silver: 3 meses** (Stéphane, 22-11). ⬜ Implementar no `lake_housekeeping` junto com o gold: um dia só
     sai se o gold dele estiver numa release.
   - ⬜ Medir os metadados por dia uma semana depois de implantar.
37. 🟡 (2026-11-23) **Revisão de production readiness: 87/100** (agente, só de leitura). Pronto para o âmbito, com prazo:
   silver, metadados do lakeFS e tempo da GC crescem sem teto.
   - ✅ **Plataforma `3fce2c8`, em produção:** o alarme da GC conta um ciclo feito à mão (evitou um falso alarme a 24-11);
     horas aceites no alarme da silver (`SILVER_ACCEPTED_GAPS`); repetições e prazo nos 11 pedidos ao IMDS; o token das
     tarefas do Airflow dura 30 min e os pods falhados são apagados; alarme de pedidos `[1h] > 200` (`terraform apply`, 1 recurso);
     runbook. Testado na Oracle: os 3 verificadores correram bem.
   - **No disco, por rever:** OpenMetadata atualiza o resultado quando uma hora se repete (409 → PATCH; provado no
     OpenMetadata local); o simulador esquece cenários acabados há mais de 1 h; ADR-020 com as contas corrigidas
     (dumps ~2,3 GB, bronze ~1,7 GB; data limite da retenção da silver: fevereiro de 2027); ADR-019 com a silver às :25.
   - ✅ **ADR-021 aceite** (23-11): um repositório lakeFS por ano, a 1 de janeiro (o único teto para os metadados e a GC);
     o antigo fica 30 dias só de leitura (sem GC, fora da cópia) e depois apaga-se; primeira rotação a **1-1-2027**.
     ⬜ Antes disso: `scripts/lakefs-rotate.sh` com ensaio, o nome do repositório numa só variável, ensaio no local,
     runbook, lembrete no início de dezembro.
38. 🟡 (2026-11-23) **Fase 6: gold, G3 e T1 no disco, por rever** (ADR-019 §8 e §8.1). T1 (a silver regista o commit de
   bronze), gold K3 (9 tabelas por dia, ~2,2 s e pico 382 MB num dia real), G3 (6 regras) e os hooks, a execução diária e a
   DAG `gold_daily` (02:40; 1.º dia 23-11). Escolhas do Stéphane aplicadas: parado = 0 kW, teto por sinal (300 s nos
   lentos), `memory_limit` 256 MB. **Achado:** dois hooks Lua em paralelo derrubam o lakeFS 1.86; G2 e G3 passam a ser
   hooks da mesma ação. Testes: 91 + 17 saltados; 108/108 contra o lakeFS real do local.
   - ⬜ Releases (tag + manifest), `LATEST_STABLE` com hook e rollback < 1 min; OpenMetadata (M1); retenção da silver.
