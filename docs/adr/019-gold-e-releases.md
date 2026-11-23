# ADR-019: gold, gate G3 e releases (Fase 6)

**Estado:** escolhas feitas (2026-11-22, secção 7). D11 (Fase 6a) implementado no disco, por rever. **2026-11-23, no
disco, por rever (sem commit):** T1 (D8), o gold K3 (`pipelines/src/gold/`), o G3 e os 3 hooks (não instalados), o
`run_day` e o DAG `gold_daily`; ver a secção 8. Falta: releases, `LATEST_STABLE`, OpenMetadata.
**Liga a:** PLAN §5.4 (gold), §6 (G3), §7 (release tag), Fase 6; ADR-007 (silver), ADR-008 (gates), ADR-009
(OpenMetadata), ADR-015 (retenção e GC), ADR-016 (cópia em pacotes), ADR-018 (precisão do relógio).

## 1. Contexto

A silver (`silver/dt=…/hour=…/data.parquet`, ~43 mil linhas por hora) está no `main`, hora a hora, com o G2 a impor a
qualidade. Falta a última camada do Write-Audit-Publish: **o gold** (números de negócio), **o G3** (o porteiro do
gold) e **as releases** (uma versão imutável a que os consumidores se agarram, com rollback).

DoD da Fase 6 (PLAN): **≥3 releases; um rollback demonstrado em < 1 min; a partir do manifest reconstrói-se que
commit de bronze originou uma release.**

### O que os dados permitem (verificado no código do simulador e no `catalog.yaml`)

| Ativo | Sinais úteis para KPIs | O que **não** há |
|---|---|---|
| PUMP-01 | caudal (m³/h), pressão, corrente (A), estado, alarme, `run_hours` | **potência**: não há sinal de kW |
| MOTOR-01 | rpm, binário (N·m), corrente, estado, alarme | caudal |
| CHILLER-01 | potência (kW), temperaturas de entrada e saída, estado | caudal de água, alarme |

Três factos que pesam nas escolhas:
1. **Consumo específico da bomba (kWh/m³)**, pedido no PLAN e no DoD da Fase 7, precisa de energia elétrica, e a bomba
   só dá corrente. Ou se estima (P = √3 · U · I · cos φ, com U e cos φ assumidos), ou se acrescenta um sinal.
2. **O simulador quase nunca avaria.** Só entra em `FAULT` no fim de um `drift`, e em `STARTING` num `power_cycle`. Nunca
   fica `STOPPED` nem em `MAINTENANCE`. Sem cenários injetados, a disponibilidade dá ~100% e o MTBF fica "sem falhas".
   Isto não é um erro: é o que se vê numa fábrica boa. Para a demonstração, injetam-se cenários (como na Fase 5).
3. **O gateway só escreve quando o valor muda.** Uma média simples das linhas engana (um valor que fica parado 10 min
   conta como 1 linha). Médias, energia e volume têm de ser **ponderados pelo tempo**: cada valor vale até à leitura
   seguinte, com um teto (por omissão 60 s para analógicos; para o estado, até ao próximo estado ou até à perda de
   comunicação). Acima do teto, o tempo é **"sem dados"**, nunca é inventado.

### A hora do evento e a hora de receção

A silver está partida pela hora de **receção** (ADR-007); o gold agrega pela hora do **evento** (`ts_utc`). Uma leitura
das 23:59:59 pode estar na partição da 00h do dia seguinte. Por isso o gold do dia D lê as partições de silver de
**D−1 23h a D+1 01h** e filtra `ts_utc` dentro de D. Leituras que cheguem ainda mais tarde ficam de fora dessa release
e entram num reprocessamento (release PATCH). Linhas com a flag `clock_skew` usam o `server_ts` como tempo (o relógio do
equipamento está errado; o do gateway não).

## 2. Decisões e opções

Para cada decisão há opções e uma recomendação **(R)**. O Stéphane escolhe; o que não escolher fica com a recomendação.

### D1 — Que tabelas e KPIs

| Opção | Tabelas | KPIs | Tamanho |
|---|---|---|---|
| **K1 — mínimo** | `dim_asset`, `dim_tag`, `fact_tag_hour` (por ativo, tag e hora: amostras, % GOOD, minutos com dados, mín., máx., média ponderada, desvio, primeiro, último) | cobertura de dados, energia do chiller | ~550 linhas/dia |
| **K2 — negócio (R)** | K1 + `fact_asset_hour` (segundos em cada estado, segundos sem dados, falhas, alarmes, energia, volume) + `fact_downtime` (intervalos de paragem com causa) + `kpi_asset_shift` e `kpi_asset_day` | **disponibilidade, MTBF, MTTR, taxa de alarmes, consumo específico** (kWh/m³ da bomba), energia por ativo | ~700 linhas/dia |
| **K3 — tudo do §5.4** | K2 + `fact_tag_minute` (por minuto) | os mesmos, com detalhe ao minuto para o BI | ~37 mil linhas/dia |

As features de ML (`feature_asset_5min`) ficam para a Fase 8, em qualquer opção: só fazem sentido quando houver modelo.

**Definições propostas (K2):**
- **Disponibilidade** = tempo a funcionar (`RUNNING`; `COOLING` no chiller) ÷ (tempo total − tempo sem dados). O tempo
  sem dados (ex.: `comms_gap`) **não conta contra o ativo** e mostra-se à parte como "cobertura". Alternativa: contá-lo
  como paragem (mais severo). Não há plano de produção, por isso o tempo planeado é 24 h.
- **Falha** = entrada em `FAULT`. **MTBF** = horas a funcionar ÷ n.º de falhas; **MTTR** = horas em `FAULT` ÷ n.º de
  falhas. Num período sem falhas, o MTBF mostra "sem falhas em N horas", não um número inventado. O MTBF "a 30 dias"
  lê os 30 dias anteriores do próprio gold (dezenas de linhas).
- **Taxa de alarmes** = n.º de vezes que o alarme passa de falso a verdadeiro, por 24 h a funcionar.
- **Energia:** chiller = ∫ potência (medida); motor = binário × velocidade (potência mecânica, física exata);
  bomba = ver D2.
- **Volume bombeado** = ∫ caudal. **Consumo específico** = energia da bomba ÷ volume.

### D2 — Consumo específico da bomba (falta o sinal de potência)

| Opção | O quê | Custo |
|---|---|---|
| **E1 — estimar pela corrente (R)** | P = √3 × 400 V × I × 0,85; U e cos φ ficam em `dim_asset`, e a coluna diz `estimated = true` | nenhum; honesto se estiver rotulado |
| **E2 — acrescentar `power_kw` à bomba** | sinal novo no simulador, no mapa de registos, no gateway, no catálogo e nos contratos (Fases 1–4) | ~meio dia; mexe em 5 contratos já fechados |
| **E3 — não ter kWh/m³** | só A·h por m³ (indicador relativo) | nenhum; falha a pergunta do DoD da Fase 7 |

### D3 — Grão de tempo e turnos

**Recomendação única (sem opção real contra):** guardar só **medidas que se somam**, por ativo e **hora UTC** (segundos
em cada estado, kWh, m³, falhas, alarmes). Qualquer KPI de turno, dia ou mês calcula-se a partir das somas (nunca média
de médias). Uma tabela `dim_time` liga cada hora UTC à data e ao turno **de Lisboa** (a mudança de hora é sempre de 1 h
inteira, por isso uma hora UTC cai sempre num só turno).
- **Turnos propostos:** 3 × 8 h, 06–14, 14–22, 22–06 (hora de Lisboa); o turno das 22 pertence ao dia em que começa.
  Muda-se num ficheiro, se o Stéphane preferir outros.

### D4 — Formato e particionamento no `industrial-lake`

Formato: **Parquet**, como o resto (decisão D4 do PLAN). Ordenado e com nome fixo, para repetir dar o mesmo ficheiro.

| Opção | Caminho | Objetos por dia (K2) | Nota |
|---|---|---|---|
| **P1 — por dia do evento (R)** | `gold/<tabela>/dt=YYYY-MM-DD/data.parquet` (as `dim_*` sem partição) | ~6 + atestação + manifest | poucos ficheiros; um reprocessamento reescreve só o dia |
| P2 — por hora | `gold/<tabela>/dt=…/hour=…/data.parquet` | ~120 | ficheiros de poucos KB: mais metadados do lakeFS e mais objetos nos pacotes, sem ganho (o gold de um dia inteiro tem < 1 MB) |
| P3 — por mês | `gold/<tabela>/month=YYYY-MM/data.parquet` | ~6 reescritos | cada dia reescreve o mês; um rollback de um dia desfaz o mês |

### D5 — Quando corre o gold e com que cadência há releases

| Opção | Como | Releases | Atraso do gold |
|---|---|---|---|
| **C1 — diário (R)** | DAG `gold_daily` às **02:40 UTC**: calcula o dia anterior (a silver da 01h fica pronta às 02:25, ADR-020), corre o G3, faz merge, cria a tag e move o `LATEST_STABLE` | 1 por dia (+ manuais) | até ~27 h |
| C2 — horário | uma tarefa a seguir a cada hora de silver; reescreve o dia em curso | 24 por dia | ~1 h |
| C3 — misto | gold de hora a hora no `main`, release só 1 vez por dia | 1 por dia | ~1 h no `main`, ~27 h na release |

O "agora" é trabalho da Fase 7 (Grafana sobre o Timescale, < 5 s), não do gold. Por isso o gold pode ser diário. Com
C1, as 3 releases do DoD fazem-se em 3 dias, ou mais depressa com releases manuais (um reprocessamento é um PATCH).
`max_active_runs=1`, e às 02:40 não corre mais nada pesado (a silver às :25, a limpeza às 03:30, a cópia às 03:00).

### D6 — O gate G3 (regras propostas)

Como o G2: **Python no DAG escreve a prova, o hook Lua do lakeFS confere-a.** Atestação
`_gates/g3/dt=YYYY-MM-DD.json`, com o checksum de cada ficheiro `gold/` do dia.

| Regra | Verifica | `warn` | `blocker` |
|---|---|---|---|
| G3-01 reconciliação | soma das amostras em `fact_tag_hour` = linhas da silver com `ts_utc` no dia, por ativo e tag | — | qualquer diferença |
| G3-02 buracos | cada ativo tem as 24 horas (uma hora sem dados aparece com cobertura 0, nunca falta a linha) | — | hora em falta |
| G3-03 física | segundos por hora = 3600 (somando os estados e o "sem dados"); disponibilidade em [0, 1]; energia e volume ≥ 0; kWh/m³ dentro de [0,05; 2] **(a calibrar)** | — | fora |
| G3-04 cobertura | horas do dia cuja silver foi **recusada pelo G2** (não estão no `main`) | qualquer | > 6 h **(a calibrar)** |
| G3-05 volume | n.º de linhas da silver do dia contra a média dos 7 dias anteriores | ±20 % | — |
| G3-06 deriva | PSI da média horária de cada tag analógica contra os 7 dias anteriores | PSI > 0,25 | — (`info` até haver histórico) |

O PSI/KS do PLAN entra como aviso e não como bloqueio: com um simulador, uma deriva é muitas vezes um cenário de
propósito (o `drift`). Bloquear deixaria o gold sem os dados que a Fase 8 precisa.

**Imposição no lakeFS (3 hooks novos, a provar num repositório descartável):**
1. `g3_pre_merge` (`pre-merge`, destino `main`): todo o ficheiro `gold/` que entra tem atestação G3, sem `fail`, com o
   checksum certo. É o `g2_pre_merge` aplicado a outro prefixo.
2. `release_pre_create_tag` (`pre-create-tag`): só aceita uma tag `gold-v*` sobre um commit em que a atestação G3 do
   dia existe e não é `fail`.
3. `release_pre_delete_tag` (`pre-delete-tag`): recusa apagar tags `gold-v*`. **No lakeFS uma tag apaga-se**; sem isto
   "imutável" seria só uma promessa.

### D7 — A release: nome, versão e manifest

- **Tag:** `gold-vMAJOR.MINOR.PATCH+YYYYMMDDHHmm`, do PLAN §7. Uma release diária de dados novos **não muda a versão**,
  só o carimbo: `gold-v1.0.0+202611230240`, `gold-v1.0.0+202611240240`. `PATCH` sobe num reprocessamento,
  `MINOR` numa tabela ou coluna nova, `MAJOR` numa quebra de esquema. A confirmar no protótipo: se o lakeFS aceita `+`
  num nome de tag; se não, usa-se `_`.
- **Manifest** em `_releases/<tag>/manifest.json`, **escrito no mesmo commit que a tag marca** (a release leva a prova
  consigo):

```json
{
  "release": "gold-v1.0.0+202611230240", "schema_version": "1.0.0",
  "covers": {"from": "2026-11-22T00:00:00Z", "to": "2026-11-23T00:00:00Z"},
  "tables": {"gold/fact_asset_hour/dt=2026-11-22/data.parquet": {"rows": 72, "checksum": "…"}},
  "code": {"git_commit": "82f3868…", "image": "ghcr.io/…/modbus-pipelines:82f3868…", "dag": "gold_daily"},
  "gates": {"g2": {"2026-11-22T00": "pass", "…": "…"}, "g3": "pass"},
  "inputs": {
    "silver_commit": "<commit do main lido pelo gold>",
    "silver_partitions": {"silver/dt=2026-11-22/hour=00/data.parquet": {"checksum": "…", "bronze_commit": "…"}}
  }
}
```

O manifest não pode conter o ID do commit em que está (seria preciso sabê-lo antes de o criar); esse ID é o da tag.

### D8 — Rastrear até ao bronze (a peça que hoje falta)

**Hoje não dá:** o `run.py` da silver lê o bronze do branch `main` **pelo nome**, que avança enquanto lê, e não guarda
de que commit leu. Sem isto o DoD ("que commit de bronze originou uma release") não se cumpre.

| Opção | Como | Custo |
|---|---|---|
| **T1 — a silver regista o commit de bronze (R)** | o `run.py` resolve `main` → ID do commit **antes** de listar, lê tudo desse commit fixo e guarda `bronze_commit` nos metadados do commit e na atestação G2. O gold copia-o para o manifest | ~15 linhas em `run.py`; o ficheiro da silver não muda (`silver_version` fica "2"); também tira a corrida "lista num commit, lê noutro" |
| T2 — reconstruir pelos lotes | o gold lê os `bronze_batch_id` distintos da silver e procura, no log do `main`, o merge de cada lote | sem mexer na silver, mas ~288 lotes por dia a procurar no log; lento e frágil |

As horas de silver feitas **antes** de T1 não têm o campo: o manifest diz `"bronze_commit": "desconhecido (silver
anterior a ADR-019)"`, ou reprocessa-se essa silver (os últimos 15 dias de bronze ainda existem).

**Limite honesto (ADR-015):** a bronze vive 15 dias e a GC apaga os objetos que nenhum commit recente usa. O **ID** do
commit de bronze fica para sempre (os commits não se apagam), mas os **ficheiros** desse bronze só se leem durante 15
dias. Rastrear sempre; reconstruir ao byte, só dentro da janela.

### D9 — O `LATEST_STABLE` e o rollback (< 1 min, sem copiar dados)

Uma tag do lakeFS não se pode mover. Por isso o "ponteiro" tem de ser outra coisa:

| Opção | O que é o ponteiro | Rollback | Imposto pela infraestrutura? | Consumidores leem |
|---|---|---|---|---|
| **S1 — ficheiro no `main` (R)** | `_releases/LATEST_STABLE` (JSON com o nome da tag), mudado por branch + commit + merge | escrever o nome da tag anterior: ~3–5 s | **sim**: um hook `pre-merge` só aceita o ficheiro se a tag existir e tiver G3 sem `fail`; cada mudança fica no `lakectl log` (quem, quando, porquê) | 2 passos: lê o ficheiro, depois lê o gold nessa tag |
| S2 — branch `stable` | um branch sem commits próprios, posto em cima da tag com *hard reset* | 1 chamada à API: < 1 s | **não**: o lakeFS não tem hook de *reset*; quem tem a chave põe o `stable` onde quiser. Sem histórico das mudanças | 1 passo: `lakefs://industrial-lake/stable/gold/…` |
| S3 — linha no PostgreSQL | uma tabela `release_pointer` na base do serving | 1 `UPDATE`: < 1 s | não; e o ponteiro vive fora do lake | 2 passos, e mais um sítio com a verdade |

Imagem: `docs/img-27-release-e-rollback.svg`.

O S1 segue a ideia do projeto (o que importa é imposto pelo lakeFS, como o G2). O S2 é mais simples de consumir, mas
perde a garantia e o histórico. **O `LATEST_STABLE` só avança com G3 `pass`** (PLAN §7). Com `warn`, a release existe mas o
ponteiro não se mexe sozinho; avança à mão se o Stéphane decidir que o aviso é aceitável.

**Janela do rollback:** as partições de dias passados não se reescrevem (exceto num reprocessamento), por isso ficam no
`main` e uma release antiga lê-se sempre. Um dia **reprocessado** só se lê na versão antiga enquanto a GC a guardar (15
dias). **A confirmar no protótipo:** se a GC do lakeFS 1.86 protege os commits com tag. Se proteger, a janela é ilimitada.

### D10 — OpenMetadata (PLAN: "tags de release refletidas no catálogo")

| Opção | Como | Nota |
|---|---|---|
| **M1 — propriedades na tabela (R)** | cada tabela gold tem `release` e `lakefs_commit` (propriedades próprias), atualizadas a cada release e rollback, e a linhagem silver → `gold_daily` → gold | não acumula lixo; o histórico das releases fica no lakeFS e no manifest |
| M2 — uma etiqueta por release | classificação `GoldRelease` com uma etiqueta por versão | ao fim de um ano são 365 etiquetas no catálogo |

Em ambas, os resultados do G3 vão para o OpenMetadata como os do G2 (melhor esforço: o OpenMetadata em baixo nunca
falha o gold).

## 3. Custo e memória (VM Oracle: 2 OCPU / 12 GB, ~5,4 GB livres)

**Nenhuma opção cria um serviço novo.** O gold é uma tarefa do Airflow que nasce, corre e desaparece (o pod tem pedido
de 384 Mi e limite de 1 Gi, os mesmos da silver). A RAM **permanente** não muda em nenhuma opção.

| Opção | RAM durante a corrida | Duração estimada | Disco por ano (lake) | Pedidos ao Object Storage | € |
|---|---|---|---|---|---|
| K1 / K2, P1, C1 | pico ~250–400 MB (DuckDB sobre ~1,2 M linhas de 27 horas de silver, com `memory_limit` de 512 MB), 1 vez por dia | ~10–30 s | ~20–40 MB | 0 a mais (os ficheiros entram nos pacotes da noite, ~6 pedidos como hoje) | 0 |
| K3 (+ minuto) | igual | ~20–40 s | ~0,4–0,7 GB (×2,5 na cópia ≈ 1,5 GB, dentro dos 20 GB grátis) | 0 a mais | 0 |
| C2 (horário) | igual, 24 vezes por dia | ~24 × 10 s | igual a C1, mais metadados do lakeFS (48 commits/dia, contra 288 do bronze) | 0 a mais | 0 |
| S1 / S2 / S3 | ~0 (uma chamada à API) | < 5 s | bytes | 0 | 0 |
| M1 / M2 | ~0 no Airflow; o OpenMetadata já corre | — | — | 0 | 0 |
| Hooks G3 e tags | ~0 (Lua dentro do lakeFS) | ms | — | 0 | 0 |

Estimativas por medida: uma hora de silver custa 0,07 s de CPU e 183 MB de pico (PLAN §9); o teste de carga leu 11,9 M
linhas em 137 s com um worker. O gold do dia lê ~1,2 M linhas e escreve < 1 MB.

**Medido a 2026-11-23** (no Mac; dia sintético com leituras a cada 1 s, como o gateway real: 1,12–1,26 M amostras por
dia, 26 ficheiros de silver, 31–33 MB): o `build_day` leva **1,8–2,6 s** e o G3 < 0,1 s; o pico do processo é
**~420 MB** (52 MB de Python e bibliotecas + ~370 MB do DuckDB e do Arrow), com `memory_limit` 512 MB e `threads=1`. Com
`memory_limit` 256 MB: ~380 MB, o mesmo tempo. O gold do dia tem ~0,4 MB (9 ficheiros). **Dentro do limite de 1 Gi;
acima do pedido de 384 Mi** (o pod pode passar o pedido; só o limite mata). **Decidido a 23-11: `memory_limit` 256 MB**
(medido de novo com o código final: 2,2 s, pico **382 MB**, abaixo do pedido de 384 Mi). Três mudanças do protótipo, por medida:
um ASOF JOIN com `threads=1` levava minutos (trocado por uma janela `last_value … IGNORE NULLS`, < 1 s); a grelha
calcula-se um ativo de cada vez (de ~600 para ~420 MB); o `bronze_event_id` só entra como hash (8 bytes).
Alarme novo, se for preciso: "gold sem release há > 30 h", pelo `job-freshness` (nunca uma janela `[1d]`, ADR-017 e
PLAN item 31).

## 4. Recomendação, em conjunto

*(Era a proposta inicial; as escolhas finais estão na secção 7: K3, E2 e as fontes do D11.)*
**K2 + E1 + P1 + C1 + G3 (6 regras, 3 hooks) + manifest + T1 + S1 + M1.** Custo extra: €0, RAM permanente 0, ~30 s por dia de
uma tarefa com ≤ 1 Gi. Do mais pequeno para o maior, a ordem de trabalho seria:
1. protótipo num repositório descartável: hooks `pre-create-tag`/`pre-delete-tag`, `+` no nome da tag, GC com tags;
2. T1 na silver (pequeno, e desbloqueia a rastreabilidade);
3. gold K2 com testes locais (reprocessar um dia dá os mesmos bytes; reconciliação com a silver);
4. G3 + hooks; 5. release + `LATEST_STABLE` + rollback cronometrado; 6. OpenMetadata; 7. DoD na Oracle (3 releases,
rollback < 1 min, reconstrução do bronze a partir de um manifest).

## 5. Alternativas rejeitadas à partida
- **Iceberg ou Delta para o gold:** o lakeFS já dá atomicidade e histórico (decisão D4); seria outra camada de conceitos.
- **Gold no PostgreSQL em vez do lake:** perdia as releases e o rollback do lakeFS. O PostgreSQL é o destino da Fase 7,
  sincronizado a partir do `LATEST_STABLE`.
- **Tag "móvel" (apagar e criar outra vez `latest-stable`):** não é atómico. Entre apagar e criar, os consumidores
  falham. E o hook D6.3 impede apagar tags.

## 6. Consequências
- A silver passa a guardar o commit de bronze (T1); o gold e as releases ficam ligados até ao bronze por IDs.
- O gold está ~1 dia atrasado; o "agora" é o caminho live da Fase 7.
- O consumo específico da bomba é **estimado** e está rotulado assim (E1).
- Os KPIs de falhas só têm significado com cenários injetados (o simulador não avaria sozinho).
- Mais 3 hooks Lua para manter; o limite do ADR-008 mantém-se: quem tem a chave de administrador pode contorná-los de
  propósito.

## 7. Escolhas do Stéphane (2026-11-22)

| Decisão | Escolha |
|---|---|
| D1 KPIs | **K3** (tudo do §5.4, com a tabela ao minuto), e **fontes mais completas**: acrescentar o que for preciso ao simulador, ao gateway, ao ingest e ao resto, sempre a custo zero (ver D11) |
| D2 bomba | **E2**: sinal novo `power_kw` na bomba (medido, não estimado) |
| D5 cadência | **C1**: diário às 02:40 UTC |
| D9 ponteiro | **S1**: `_releases/LATEST_STABLE` no `main`, imposto por hook |
| restantes | ficam com a recomendação (D3 hora UTC + turnos de Lisboa, D4 P1, D6 G3 com 6 regras e 3 hooks, D7, D8 T1, D10 M1) até o Stéphane dizer o contrário |

### D11 — Fontes mais completas (proposta, à espera de confirmação da lista)

Cada sinal novo atravessa toda a cadeia: simulador (`spec.py`) → `docs/register-map.md` → gateway (`devices.yaml`,
`docs/opcua-address-space.md`) → ingest (`nodes.yaml`, o G1 conhece os nós) → `catalog.yaml` da silver → contratos →
testes. Reabre o DoD das Fases 1 e 2 para esses sinais (ler por RS-232/TCP com a ordem de palavras certa; ver no OPC UA).

| # | O quê | Dá ao gold | Linhas a mais por hora |
|---|---|---|---|
| F1 | `PUMP-01.power_kw` (float32, 1 s) | **kWh/m³ medido** (E2) | +3 600 |
| F2 | `PUMP-01.inlet_pressure` (bar, 5 s) | Δp → potência hidráulica = caudal × Δp → **rendimento da bomba** (hidráulica ÷ elétrica) | +720 |
| F3 | `MOTOR-01.power_kw` (elétrica, 1 s) | **rendimento do motor** (binário × velocidade ÷ elétrica), energia medida | +3 600 |
| F4 | `CHILLER-01.chw_flow` (m³/h de água gelada, 5 s) | potência de frio = caudal × 4,19 × ΔT → **COP** real e kW/TR | +720 |
| F5 | cenários novos no simulador: `trip` (avaria súbita: `FAULT` N min), `planned_stop` (`STOPPED`), `maintenance` (`MAINTENANCE`), e uma **taxa de avarias aleatória** configurável (ex.: média de 1 por ativo e por dia) | **MTBF e MTTR com eventos reais**; disponibilidade que separa paragem planeada de avaria; dados com falhas para a Fase 8 | ~0 (só mudanças de estado) |
| F6 | plano de produção num ficheiro (`plan.yaml`: horas planeadas por turno, paragens planeadas) | disponibilidade sobre o **tempo planeado** e não sobre 24 h | 0 |

**O que fica de fora de propósito:** OEE completo (precisa de unidades produzidas boas e más; uma bomba não as tem).

**Efeitos laterais a tratar junto:**
- Com F5, um ativo parado tem valores constantes. O gateway só escreve ao mudar, por isso o **G2-06 (*stuck*) daria um
  falso aviso**. O G2-06 passa a ignorar ativos que não estejam a funcionar. O G2-01 (completude) deve contar o
  `state`/`device_clock` como "tem dados" num ativo parado.
- **Volume (+~8 600 linhas por hora, ~+20%):** silver e lake ~+20% (a bronze está limitada a 15 dias; a silver cresce
  para sempre), WAL da spool ~+20%, pico do pod da silver ~183 → ~220 MB, fila do collector ~+20% numa falha longa.
  Continua dentro do grátis: a cópia em pacotes não depende do n.º de linhas, e o disco está a 1–2%. Os alarmes de 12 GB
  (cópia) e 15 GB (Object Storage) avisam antes de haver custo. **A medir depois de ligar.**
- Os sinais com scan de 5 s (F2, F4) são de propósito, para pesar menos; mudam devagar.

**Recomendação (pedida pelo Stéphane):** os seis. O custo é trabalho, não dinheiro, e cada um desbloqueia um KPI que
de outra forma seria estimado ou vazio. Por ordem de valor: **F5** (sem avarias, o MTBF e o MTTR ficam vazios, e a
Fase 8 precisa de falhas), **F1** (a escolha E2), **F4** (o COP é o KPI principal de um chiller), **F6** (custo quase
zero), **F3**, **F2**. Se for preciso cortar, corta-se o F2 primeiro (o menor ganho).

**Decidido (Stéphane, 2026-11-22):** os seis.

**Implementado no disco (2026-11-22, por rever, sem commit):**
- simulador: os 4 sinais, as potências derivadas, o comportamento parado (secção 7.1 do mapa de registos), os cenários
  `trip`/`planned_stop`/`maintenance`, as avarias aleatórias (`--trip-rate-per-day`) e o plano (`plan.yaml`,
  `--plan builtin`); `run_hours` só conta a funcionar; o `drift` também para a máquina ao chegar à falha;
- gateway (`devices.yaml`), ingest (`nodes.yaml`), catálogo da silver (+ `moves_with_run`), G2-06, contratos;
- testes: simulador 56, gateway 23, pipelines 48 (+14 que precisam do lakeFS), ingest 15 (+21 que precisam do Postgres).
  Os sinais novos leem-se por um cliente Modbus independente em TCP e RS-232 (DoD da Fase 1) e por um cliente OPC UA
  com unidades e `EURange` (DoD da Fase 2).
- **Por fazer depois do commit:** imagens no CI; na plataforma, `PLAN=builtin` e `TRIP_RATE_PER_DAY=1` no `ot-edge`;
  medir o volume real (+~20% previsto) e confirmar que a silver e o G2 correm limpos com os sinais novos.

**Ordem proposta:** fazer F1–F6 primeiro (uma "Fase 6a", com o DoD das Fases 1 e 2 repetido para os sinais novos e a
silver a correr com eles), e só depois o gold K3 em cima de dados que já os têm.

## 8. O que ficou implementado (2026-11-23, no disco, por rever)

**T1 (D8).** `silver.run.run_hour` resolve o `main` para um ID de commit antes de listar o bronze (`Lake.resolve`), lê
a lista e os ficheiros desse commit e guarda-o como `bronze_commit` nos metadados do commit e na atestação G2. Repetir
uma hora mantém o commit da primeira prova se a silver sair igual (sem isto, cada repetição faria um commit novo só
porque o `main` avançou). O ficheiro da silver não muda (`silver_version` "2"); o hook do G2 não muda.

**Gold K3** (`pipelines/src/gold/build.py`, `plan.py`, `plan.yaml`; o `run.py` faz o lakeFS). As 9 tabelas, todas em
`gold/<tabela>/dt=D/data.parquet` (as `dim_*` também, ver abaixo). Como se calcula (tudo nos comentários do código):
- o dia é uma **grelha de 86 400 s**: cada sinal vale a última leitura, no máximo 60 s nos analógicos e no
  `device_clock`; estado, alarme, contadores e `Connected` valem até à leitura seguinte, e os três primeiros só com
  comunicação (o `device_clock` do ativo com dados). Uma leitura `BAD` acaba o valor. Médias, energia e volume são
  somas sobre os segundos com dados, e cada hora soma 3600 s;
- **estado inicial**: os sinais "até à leitura seguinte" podem estar dias sem linhas; o dia D começa com o valor em
  vigor no fim de D−1 (`value_end` do `fact_tag_hour` de D−1). Por isso os dias correm por ordem;
- **KPIs de turno e de dia de produção** ficam na partição do dia UTC em que o período **acaba** (o turno C e o dia de
  produção acabam às 06:00 de Lisboa do dia seguinte); leem as horas de D−1 do `fact_asset_hour` de D−1;
  `complete = false` se faltarem horas;
- **energia útil** por ativo (bomba: caudal × Δp ÷ 36; motor: binário × rpm × 2π ÷ 60 ÷ 1000; chiller:
  caudal × 4,19 × ΔT ÷ 3,6), somada só nos segundos em que também há potência: o rendimento e o COP são somas ÷ somas.

**Desvios ao texto do ADR (para o Stéphane confirmar):**
- as `dim_*` têm partição por dia (D4 dizia "sem partição"): cada ficheiro gold pertence a uma só atestação G3 e o
  hook fica igual ao do G2; custa 2 ficheiros de poucos KB por dia;
- o G3-03 ganhou "rendimento em ]0, 1]" e "COP em ]0, 12]" (física, não estava no D6);
- G3-06 sai de `info` com **3** dias de histórico (o D6 não dizia quantos).

**G3** (`pipelines/src/gold/g3.py`; contrato em `docs/gates-contract.md`, secção 6) e os hooks
`quality/hooks/g3_pre_merge.lua`, `release_pre_create_tag.lua`, `release_pre_delete_tag.lua` (em `GOLD_ACTIONS` do
instalador, **não instalados**; o teste no lakeFS real está escrito mas não correu).

**`run_day` e o DAG `gold_daily`** (02:40 UTC, `max_active_runs=1`, `catchup=True` a partir de 24-11). Sem releases.

### 8.1 Escolhas do Stéphane sobre o que o protótipo perguntou (2026-11-23)
1. **Ativo parado:** os sinais de processo (`moves_with_run`) valem 0 até à leitura seguinte enquanto há comunicação
   (parado é 0 kW, não "sem dados"). Implementado em `gold/build.py` (`_hold`, `needs_clock`); teste: o chiller parado
   dá 0,0 kW com as horas cobertas.
2. **Teto por sinal:** `hold_s` no catálogo da silver; 300 s para as temperaturas lentas e a aspiração da bomba (60 s
   os outros). Teste: a `inlet_temp` do chiller fica coberta > 99% do dia.
3. **Memória:** `memory_limit` 256 MB (pico 382 MB, dentro do pedido do pod).
4. **Início:** a DAG corre a 24-11 e o 1.º dia calculado é **23-11** (o 1.º dia completo com os sinais novos); os
   dias antes disso não têm potência. Sem backfill.
5. **As decisões do protótipo** (dimensões por dia, estado inicial de D−1 e dias por ordem, KPIs de turno no dia em que
   o turno acaba, limites do rendimento e do COP no G3-03, kWh/m³ só com potência): aceites.
6. **Hooks provados no lakeFS real** (cluster local, repositório descartável): G3 no merge e as tags de release, com
   uma correção pelo caminho: em ações separadas, o G2 e o G3 corriam em paralelo e derrubavam o lakeFS 1.86
   ("concurrent map writes"); agora são dois hooks da mesma ação, por ordem (ver `docs/gates-contract.md` §4). Suite
   dos pipelines contra o lakeFS real: 108/108.

### 8.2 Releases, LATEST_STABLE e rollback (2026-11-23, no disco, por rever)
- `pipelines/src/gold/release.py`: `release_day` (branch → manifest → commit → merge → tag no commit do merge → com G3
  `pass`, o ponteiro), `point`, `rollback`, `trace` e uma linha de comandos (`python -m gold.release …`). A DAG
  `gold_daily` faz a release logo a seguir ao gold do dia.
- **Nome das tags: `gold-vMAJOR.MINOR.PATCH_AAAAMMDDHHmm`** (com `_`, a alternativa prevista no D7). O lakeFS aceita `+`
  numa tag, mas o cliente Lua dos hooks não o escapa no endereço: o `get_object` pela tag dava 400 e o hook do ponteiro
  recusava uma release que existia (medido no lakeFS real).
- **Versão:** MAJOR.MINOR do esquema (`SCHEMA_VERSION = "1.0"`); PATCH = releases anteriores do mesmo dia (um
  reprocessamento). Repetir o mesmo gold não cria outra release. Repetir um dia antigo nunca faz recuar o ponteiro.
- **Hook do ponteiro** (`quality/hooks/release_pointer_pre_merge.lua`): 3.º hook da ação `g2` (por ordem, nunca em
  paralelo). Só aceita um `LATEST_STABLE` que nomeia uma release existente com G3 sem `fail` no manifest.
- **Provado no lakeFS real** (repositório descartável, todos os hooks): release, PATCH depois de um reprocessamento,
  **rollback em 0,12 s**, o hook a recusar um ponteiro para uma release que não existe, o rastreio até ao bronze, e o gold
  lido **pela tag na porta S3** (como os consumidores da Fase 7). Pipelines: 99 + 18 saltados; 117/117 com o lakeFS real.
- **Falta para o DoD da Fase 6:** implantar (hooks no `industrial-lake`, imagem, DAG) e, na produção, ≥3 releases reais,
  um rollback cronometrado e o `trace` de uma release. Depois: OpenMetadata (M1) e a retenção da silver.

### 8.3 OpenMetadata do gold e retenção da silver (2026-11-23, no disco, por rever)
- `pipelines/src/gold/openmetadata.py` (M1): o esquema `gold` com as 9 tabelas (colunas lidas do parquet do dia), a
  linhagem silver.readings → `gold_daily` → cada tabela, os 6 casos de teste do G3 em `gold.fact_tag_hour` (um
  resultado por dia; repetir o dia atualiza), e as propriedades `release` e `lakefs_commit` em cada tabela, mudadas
  sempre que o `LATEST_STABLE` se mexe (release ou rollback). A DAG publica o catálogo e o G3 antes da release. Melhor
  esforço: nunca falha o gold. Provado contra o OpenMetadata local (2.0.7, a versão da Oracle).
- `pipelines/src/gold/retention.py` (ADR-020, decisão 3): a silver sai ao fim de 92 dias, e só de um dia D com release
  do gold de D **e** de D+1 (o gold de D+1 lê a hora 23 de D); as provas dos gates ficam. Tarefa `expire_silver` no
  `lake_housekeeping`, **em simulação** (`SILVER_APPLY = False`) até rever o primeiro relatório com dias a sair
  (~2027-02-23).
