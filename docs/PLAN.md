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

Cada linha vira um ADR curto em `docs/adr/`.

| # | Decisão | Escolha | Alternativas | Porquê |
|---|---|---|---|---|
| 001 | Runtime k8s | **k3s numa VM Linux (Lima)** | k3d em Docker, Pi cluster | k3s a sério, storage e rede realistas, reprodutível |
| 002 | Object storage | **SeaweedFS** (MinIO deixou de publicar imagens; ver ADR-002) | MinIO, Ceph | API S3, Apache-2.0, leve |
| 003 | Versionamento de dados | **lakeFS** *(requisito)* | Iceberg, Delta, nada | Branches, tags imutáveis e hooks pré-merge — é o motor dos gates e das releases |
| 004 | Formato de tabela | **Parquet + partições Hive** | Iceberg, Delta | O lakeFS já dá atomicidade, time travel e histórico. Somar Iceberg duplicaria conceitos e triplicaria a fricção de integração |
| 005 | Catálogo e governança | **OpenMetadata** *(requisito)* | DataHub, Amundsen | Catálogo, linhagem, glossário, testes de qualidade e profiler num só sítio |
| 006 | Motor de qualidade | **OpenMetadata Data Quality** + hook lakeFS a impor | Soda, Great Expectations | Uma ferramenta só; resultados já aterram no catálogo, com histórico |
| 007 | Fronteira OT/IT | **Servidor OPC UA** | Modbus TCP direto | Tipos, unidades, quality codes, subscriptions |
| 008 | Base de dados | **PostgreSQL** *(requisito)* + TimescaleDB | ClickHouse, DuckDB servido | Backend de Airflow, lakeFS, OpenMetadata **e** serving layer. Timescale para o caminho live |
| 009 | Transporte | **Sem broker na v1** (spool local no collector) | Redpanda/Kafka, NATS | Ver §4.1 |
| 010 | Engine de transformação | **DuckDB** (lê via gateway S3 do lakeFS) | Spark, Polars, Trino | O volume não justifica Spark; DuckDB lê Parquet no S3 depressa e cabe em qualquer pod |
| 011 | Orquestração | **Airflow 3, KubernetesExecutor** *(requisito)* | Dagster, Prefect | Confirmado na stack de referência; integra com lakeFS (hooks/operadores) e alimenta a linhagem do OpenMetadata |
| 012 | Dashboard live | **Grafana** (fonte: Timescale) | app própria | Refresh curto, alerting incluído |
| 013 | BI | **Superset** | Metabase, só Grafana | Camada semântica, exploração ad-hoc sobre gold |
| 014 | ML tracking | **MLflow** | W&B (cloud → viola on-prem) | On-prem, registry, artefactos no SeaweedFS |
| 015 | GitOps | **Argo CD** | Flux, kubectl à mão | Estado declarativo, drift detection |

> **Regra:** antes de escrever o ADR, o Stéphane defende a escolha em duas frases. Se não
> conseguir defender, não está pronto para decidir — investiga primeiro.

### 4.1 Nota sobre o broker (ADR-009)

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

> **Estado real (2026-11-08).** Feito e verificado: collector OPC UA, spool, writer idempotente,
> gate G1 (8 regras) e reconciliação spool↔lakeFS sem faltas nem duplicados no cluster
> (27 116 amostras), incluindo mortes do writer e um reinício do Postgres. **Desvios:** o spool é
> PostgreSQL e não um ficheiro (ADR-006); o lote tem 3 estados. **Em falta para fechar o DoD:**
> as 24 h contínuas, o G1 provado no cluster com uma amostra inválida (só o está contra o lakeFS
> real, num destino descartável), e o caminho *live* para o Timescale. **Adiado:** comandos
> para listar e reprocessar lotes rejeitados, e G1 por linha em vez de por lote.

### Fase 4 — Silver + Airflow  🔵 em curso
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
> **Estado (2026-11-07):** feitos o transform (DuckDB), `run_hour` (branch → verificar → merge), 24 testes
> (incl. semana de 7 dias: backfill == incremental, byte a byte, 169 ficheiros; crash em cada passo deixa
> `main` intacto) e o DAG `silver_hourly` a correr no cluster (horas 16 e 17 de 07-11 reconciliadas).
> Falta: teste de integração contra um lakeFS real com branches descartáveis, backfill pelo Airflow,
> política de retenção dos branches `etl-*`/`silver-*`.

### Fase 5 — Governança e gates
OpenMetadata no cluster, conectores (PostgreSQL, datalake sobre SeaweedFS/lakeFS, linhagem do
Airflow), glossário de negócio, *owners* por dataset, test suites de G2/G3, e os *pre-merge
hooks* do lakeFS a impor o resultado.
**DoD:** injetar `comms_gap` faz G2 falhar por completude e **o merge é rejeitado**;
injetar `spike` gera `warn` e o merge passa; ambos ficam com histórico visível no
OpenMetadata; a linhagem no catálogo mostra o caminho OPC UA → bronze → silver → gold.

### Fase 6 — Gold + release tagging (Publish)
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
Prometheus/Loki, alerting, CI (lint, testes, build de imagens), Argo CD, secrets (SOPS ou
sealed-secrets), backups (versioning SeaweedFS + dump Postgres + garbage collection do lakeFS),
README com arquitetura e demo, bootstrap de raiz. Broker (ADR-009) se o gatilho se verificar.
**DoD:** destruir a VM e reconstruir tudo a partir do repo em <30 min, sem passos manuais
não documentados.

---

## 9. Orçamento de recursos (VM: 24 GB RAM)

O OpenMetadata pesa (servidor + índice de pesquisa). Por isso a plataforma corre por
**perfis**, não toda de uma vez:

| Perfil | Componentes | RAM aprox. |
|---|---|---|
| `core` (F0-F4) | k3s, SeaweedFS, lakeFS, Postgres, Airflow, OT sim | ~11 GB |
| `core+gov` (F5-F6) | + OpenMetadata + índice de pesquisa | ~16 GB |
| `core+bi` (F7) | + Grafana, Prometheus, Loki, Superset | ~17 GB |
| `full` (F8-F9) | + MLflow (OpenMetadata pode ficar parado) | ~19 GB |

Componente a componente: k3s 2 · SeaweedFS 2 · Postgres 2 · lakeFS 1 · Airflow 4 ·
OpenMetadata 2 · índice de pesquisa 2 (heap reduzido) · Grafana+Prom+Loki 2 ·
Superset 2 · MLflow 1 · folga para jobs 2-3.

**Regra:** um perfil é um conjunto de recursos do Argo CD, não um `kubectl apply` manual.

---

## 10. Riscos

| Risco | Mitigação |
|---|---|
| Âmbito enorme → desistência a meio | Cada fase entrega algo demonstrável; nunca duas fases abertas |
| OpenMetadata come a VM | Perfis (§9); heap do índice reduzido; parar governança durante trabalho de ML |
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
Não escrevo nada disto. Quando encalhares num dos 9 pontos, traz-me o erro e os ficheiros —
eu digo-te onde olhar e porquê, e faço-te perguntas até perceberes o modelo do lakeFS a
sério. Quando o ADR-000 estiver escrito, critico-o.
