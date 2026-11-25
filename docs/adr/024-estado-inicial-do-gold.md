# ADR-024: estado inicial do gold e ordem dentro do segundo

**Estado:** proposto (2026-11-25), no disco, por rever pelo Stéphane. Corrige o ADR-019 (K3, "estado inicial").

## 1. Contexto: o MOTOR-01 "sem dados" na Oracle

O dashboard de qualidade (ADR-023) mostrou o MOTOR-01 com `coverage` 0 e `no_data_s` 86 400 no `kpi_asset_day` dos
dias de produção 22 e 23-11. Mas as leituras do motor estavam todas GOOD (30 kW, 1480 rpm o dia todo), e o G3 deu `pass`.

O que se mediu (Oracle, só leitura, 25-11):

| Prova | Resultado |
|---|---|
| `events` da spool (`ingest`), todas as leituras de `*.state` | o MOTOR-01 escreveu o estado em 21-11 10:30 (RUNNING), em 22-11 22:29:08 (um BAD e logo RUNNING, ao religar) e depois só em 24-11 10:50 (FAULT → STARTING → RUNNING) |
| `serving.releases` | o 1.º dia do gold é 23-11 (`gold-v1.0.0_202611240240`); não há gold de 22-11 |
| `gold.fact_tag_hour`, `MOTOR-01.state` | 0 amostras e 0 s com dados de 23-11 00h a 24-11 10h; depois 3600 s/h |
| `gold.fact_asset_hour`, MOTOR-01 | `no_data_s` 3600 em todas as horas até 24-11 10h; a seguir `run_s` 3600 |

O simulador e o gateway estão bem: o estado só se escreve quando muda, como está previsto (ADR-019, contexto).

### Causa 1: o estado inicial só vinha do gold de D−1

O `build_day` lê a silver de D−1 23h a D+1 01h. Um sinal "até à leitura seguinte" sem leituras nessa janela só
recebia o estado do `value_end` da hora 23 do gold de D−1.
- **23-11, o 1.º dia:** não havia D−1. A última leitura do motor era de 22-11 22:29, uma hora antes da janela. A
  bomba e o chiller tiveram sorte: mudaram de estado às 23:54 e às 23:12, dentro da janela.
- **24-11:** o `value_end` do motor em 23-11 23h era nulo. O "sem dados" passou para o dia seguinte, e continuaria
  assim até o motor mudar de estado.

### Causa 2: dentro do mesmo segundo, o BAD ganhava

Às 22:29:08 o gateway mandou `MOTOR-01.state` BAD com o `ts_utc` = `server_ts` = .628 (é ele que carimba, com ms) e
depois RUNNING com o `ts_utc` .000 (o relógio do PLC, precisão de 1 s) e o `server_ts` .843. O gold escolhia "a última
do segundo" pelo instante, por isso o BAD ganhava. Isto contraria o ADR-018 (decisão 4: dentro do mesmo segundo manda
o `server_ts`). Mesmo com a causa 1 corrigida, o motor ficava "sem dados" por causa disto.

### Porque o G3 não viu

Nenhuma regra cruzava "estado desconhecido" com "leituras boas". O gold estava coerente com o que tinha lido.

## 2. Opções

**A. Procurar na silver anterior, só quando falta (escolhida).** Se um sinal "até à leitura seguinte" fica sem estado
inicial, nem da janela nem do gold de D−1, lê-se a silver para trás, uma hora de receção de cada vez, a partir de
D−1 22h. Para quando todos têm leitura, ou ao fim de 7 dias (`LOOKBACK_HOURS`).
- Custo: no dia a dia são 0 pedidos, porque o gold de D−1 tem o `value_end`. No 1.º dia, ou depois de um buraco no
  gold, são algumas horas. No pior caso são 168 GET (0,3 % dos 50 k grátis por mês).
- Um sinal que esteve calado o dia D−1 inteiro (sem amostras nem segundos com dados) não se procura outra vez, porque
  o build de D−1 já o procurou. Sem esta regra, o `Connected` (que só se escreve ao arrancar) custava 168 pedidos por
  dia.
- O esquema não muda, e o caso em que a comunicação cai no último segundo também fica coberto.

**B. Uma tabela nova `gold/last_known/dt=D`** com a última leitura de cada sinal. É mais explícito, mas muda o esquema
(é um MINOR no ADR-019 D7). Também mexe no serving, no OpenMetadata e no hook G3, e não resolve o 1.º dia: esse
continua a precisar da silver.

**C. Começar o gold num dia com estado conhecido, ou mandar o gateway republicar todos os estados de hora a hora.**
A primeira opção resolve só o arranque. A segunda muda o OT por causa de um problema do gold e põe mais amostras na
spool.

## 3. Decisão (proposta)

1. **Opção A** em `gold/build.py` (`_look_back`, `LOOKBACK_HOURS = 168`) e `gold/run.py` (o `lookback` lê do mesmo
   commit, com uma listagem por dia).
2. **Ordem dentro do segundo pelo `server_ts`**, tanto na escolha da leitura do segundo como no primeiro e no último
   valor: `{segundo, server_ts, instante, id}`.
3. **G3-07 "estado desconhecido" (`warn`).** Dispara quando um ativo tem o dia inteiro "sem dados" no
   `fact_asset_hour`, mas tem leituras GOOD no `fact_tag_hour`. É um aviso, não um bloqueio, porque o gold pode estar
   certo (por exemplo, um ativo novo cujo estado ainda ninguém leu). Mas aparece no resumo da atestação e no
   OpenMetadata.
4. `GOLD_VERSION` "1" → "2": o código mudou e os dias já feitos saem diferentes.

## 4. Consequências e o que falta (para o Stéphane)

- Testes novos em `pipelines/tests`: lookback no 1.º dia (igual ao cálculo por ordem), 0 leituras com D−1, `value_end`
  perdido contra um sinal calado, BAD contra GOOD no mesmo segundo, `run_day` no 1.º dia e G3-07. Com o código antigo,
  3 deles falham como na Oracle (BAD: `(1800, 1800)` em vez de `(3600, 0)`). Resultado: 114 passam e 24 saltam (esses
  precisam do lakeFS real).
- **Reprocessar na Oracle, por ordem, 23-11 e depois 24-11** (depois do commit, da imagem e da promoção). Pelo ADR-019
  D7 isso dá releases `gold-v1.0.1_…`, e a tarefa `serve` atualiza a base `serving`. Antes disso, confirmar que a
  `silver/dt=2026-11-22/hour=22` está no `main` (a retenção é de 92 dias, por isso devia estar).
- O G3-07 fica no `docs/gates-contract.md`. O PLAN regista a correção na Fase 7.
