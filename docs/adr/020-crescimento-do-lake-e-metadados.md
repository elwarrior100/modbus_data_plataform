# ADR-020: crescimento do lake, metadados do lakeFS e retenção da silver

**Estado:** aceite (2026-11-22). Decisão 1 implementada e testada; decisão 3: **3 meses** (Stéphane), implementa-se
junto com o gold. **Liga a:** ADR-015 (retenção e GC), ADR-016 (cópia em pacotes), ADR-017 (corta-custos),
ADR-019 (gold e sinais novos).

## 1. Contexto: o que se mediu a 2026-11-22

A pergunta era "com os sinais novos (ADR-019, +19% de amostras) continuamos dentro do grátis?". Medido, não estimado:

| O quê | Valor | Como |
|---|---|---|
| Amostras por hora | 44 326 (Oracle) → **52 578** com os sinais novos (+18,6%) | spool, 24 h na Oracle; 10 min no cluster local |
| Silver, hora completa | **1,26 MB** (~30 MB/dia; ~36 com os sinais novos) | lake local, mediana das 15 horas maiores |
| Bronze, hora completa | **1,56 MB** (~37 MB/dia; ~45 com os sinais novos) | idem |
| SeaweedFS da Oracle | 1,31 GB: **`_lakefs` (metadados) 1,02 GB**, `data` (bronze + silver) 0,29 GB | `weed shell fs.du` |
| Metadados criados por dia | 15-11: 189 MB · 16-11: 419 MB · 17-11: 214 MB · 22-11 (até às 13h, commits a cada minuto): ~85 MB | filer do SeaweedFS, por data de criação |
| Metadados por hora, writer a 5 min (22-11 desde as 14h) | **0,2–0,8 MB/h → ~10–20 MB/dia** | idem |
| Object Storage | cópia do lake 1,78 GB · dumps do Postgres 0,95 GB · tfstate 0,1 MB | `oci os object list` |

**O que isto revela:**
1. **Os metadados do lakeFS são a maior parte do lake (~80%), e nunca se apagam.** A GC do lakeFS apaga só ficheiros de
   dados que nenhum branch ou commit recente usa; o lakeFS guarda todos os metadados (os commits não se apagam e cada um
   aponta para os seus índices: *ranges* e *metaranges*). Crescem ao ritmo dos **commits**, não das linhas. O 1 GB que
   já lá está vem sobretudo dos commits de minuto a minuto (até 22-11) e fica para sempre.
2. **A silver também nunca se apaga** (ADR-015), e cresce ~36 MB/dia.
3. **A cópia do lake no Object Storage** tem, em média, ~1,3× o lake (um completo semanal e os incrementais), mas no pico
   **~2,1×** durante 2 dias por semana (o completo novo e o anterior, que a regra de ciclo de vida só apaga aos 9 dias).
   A VM não pode apagar na cópia, de propósito (ADR-015, decisão 3), por isso o pico é inevitável sem abrir mão disso.
4. **Os sinais novos não são o problema.** Aumentam os dados em ~19%, mas os metadados não (o número de ficheiros e de
   commits por lote não muda).

Sem mudanças, daqui a 1 ano: silver ~13 GB + bronze ~1,3 + metadados ~1 + 5,5 → lake ~21 GB, cópia ~27 GB em média.
Os 20 GB grátis do Object Storage acabariam por volta de **meados de 2027** (no pico, antes disso).

## 2. Decisões

### Decisão 1 — menos commits: o writer junta lotes de **15 em 15 minutos** (implementada no disco)

Os metadados são proporcionais aos commits. Lotes de 15 min fazem **3× menos commits** do writer do que de 5 min
(96 + 96 merges por dia, contra 288 + 288). Previsão: **~3–7 MB/dia de metadados, ~1–2,5 GB por ano**.
- Plataforma: `BATCH_AGE_S=900` e `BATCH_ROWS=20000` (`projects/modbus/k8s/ingest/ingest.yaml`). O limite de linhas
  tinha de subir: a 5 000, com os sinais novos (~4 500 linhas a cada 5 min), os lotes fechariam a cada ~6 min pelo
  número de linhas e não se ganhava nada. 15 min ≈ 13 200 linhas; o teto de 20 000 só conta a recuperar um atraso.
- Projeto: a silver passa das :15 para as **:25** (`pipelines/dags/silver_hourly.py`). A última amostra de uma hora
  chega ao bronze até ~15 min depois; ficam os mesmos 10 min de margem que havia.
- **O que se perde:** o bronze chega ao `main` até 15 min depois (era 5). Não afeta o "agora", que é o caminho live da
  Fase 7. Um lote recusado pelo G1 passa a levar até 15 min de dados (era 5).
- **Verificado:** um lote de 20 000 linhas custa +47 MB de pico ao writer (fica em ~200 MB, limite 512 Mi); todos os
  testes passam, incluindo os do writer contra o PostgreSQL real (matar o writer em cada passo) e os do lakeFS real:
  ingest 36/36, pipelines 62/62, simulador 56/56, gateway 23/23. Falta: medir a taxa real de metadados uma semana depois.
- **Alternativas:** 30 min (6× menos, mas a silver só às :40 e um lote recusado leva meia hora de dados); juntar vários
  lotes num commit sem merge por lote (mexe no desenho do WAP do writer, ADR-006); deixar como está (5,5 GB/ano de
  metadados para sempre).

### Decisão 2 — a cópia do lake fica como está

Reduzir o pico de 2,1× exigiria que a VM pudesse apagar na cópia (o completo anterior logo que o novo existe), o que
desfaz a proteção contra uma VM comprometida (ADR-015). Ou exigiria uma regra de ciclo de vida mais curta, o que arrisca
apagar o único completo se o novo falhar numa noite. **Fica como está**; a retenção da silver (decisão 3) é que mantém o
lake pequeno. O que se paga é a **média** (o Object Storage cobra GB por mês, ao tempo): um pico de 2 dias por semana
acima dos 20 GB custaria cêntimos, e o corta-custos para tudo a €1.

### Decisão 3 — retenção da silver: **3 meses** (escolhido pelo Stéphane, 2026-11-22)

Com o gold (ADR-019, K3), os agregados ao minuto e à hora ficam para sempre (~0,5 GB por ano). A silver antiga pode
sair: perde-se o detalhe segundo a segundo, não os números. **Regra de segurança:** um dia de silver só se apaga se o
gold desse dia existir numa release (o manifest prova-o). Implementa-se no `lake_housekeeping`, junto com o gold.

Contas a 1 ano, já com a decisão 1 (metadados 1 + ~1,8 GB), bronze ~1,7 GB, gold ~0,5 GB, dumps ~2,3 GB (**corrigidas a 2026-11-23**, ver a secção 5):

| Silver guardada | Lake | Cópia, média (~1,3×) | Cópia, pico (~2,1×) | Total no pico (+ dumps) | Folga para os 20 GB |
|---|---|---|---|---|---|
| 6 meses (~6,5 GB) | ~11,6 GB | ~15 GB | ~24,4 GB | **~26,7 GB** | não: a média (~17 GB com os dumps) já passa o alarme dos 15 GB |
| 4 meses (~4,4 GB) | ~9,4 GB | ~12,2 GB | ~19,7 GB | **~22 GB** | não, no pico (2 dias por semana, cêntimos) |
| **3 meses (~3,3 GB)** | **~8,3 GB** | **~10,8 GB** | **~17,4 GB** | **~19,7 GB** | **à justa** ao fim de 1 ano (média ~13 GB com os dumps); o alarme dos 15 GB dispara nos 2 dias de pico |

Os metadados continuam a crescer ~1,8 GB por ano. Com 3 meses de silver, o pico chega aos 20 GB por volta do **fim de 2027**:
nessa altura revê-se (o alarme dos 15 GB avisa muito antes).

**Recomendação: 3 meses.** A Fase 8 (ML) precisa de detalhe para aprender avarias: 3 meses de silver ao segundo com o
gold ao minuto para sempre chegam, e o `drift` e as avarias são simulados (podem repetir-se quando for preciso).

### Decisão 4 — o 1 GB de metadados antigos fica

Vem dos commits de minuto a minuto, entre 14-11 e 22-11. Apagá-lo exigiria reescrever a história do lakeFS (os commits
apontam para ele). Aceita-se: é fixo e não cresce.

## 3. Consequências
- Bronze no `main` até 15 min depois; silver de cada hora às :25.
- O crescimento do lake passa a ser dominado pela silver, e a retenção dela (decisão 3) é o que nos mantém no grátis.
- A medir uma semana depois de implantar: metadados por dia (o mesmo método do filer do SeaweedFS). Se ficarem acima de
  7 MB/dia, considerar lotes de 30 min.

## 4. Fontes
- Documentação do lakeFS sobre a GC: <https://docs.lakefs.io/howto/garbage-collection/gc/> (só apaga objetos que nenhum
  branch nem commit dentro da retenção usa; os metadados ficam).

## 5. Revisão de 2026-11-23 (revisão de production readiness, 87/100)
- **Dumps do PostgreSQL:** hoje 0,95 GB, mas a spool só tem 4,1 M de linhas; cheia (7 dias, ~9 M) cada dump terá ~300 MB
  e o bucket ~2,3 GB (não 1,1). **Não** se encurtam os dumps para 5 dias: o runbook pede para retomar a VM em menos de 9
  dias depois do corta-custos, e com 5 dias uma VM parada ficava sem nenhum dump.
- **Bronze:** os 15 dias no `main` mais os 15 dias que a GC guarda o que se apagou dão ~30 dias físicos (~1,7 GB).
- **Consequência:** mesmo com a silver a 3 meses, o pico da cópia chega aos ~20 GB ao fim de 1 ano, e os metadados do
  lakeFS e o tempo da GC continuam a crescer sem teto. O teto verdadeiro é o ADR-021 (um repositório lakeFS por ano).
- **Data limite** para a retenção da silver: **fevereiro de 2027** (antes de o alarme dos 15 GB começar a disparar).
