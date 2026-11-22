# Contrato dos gates G2 e da imposição no lakeFS (Fase 5)

Define o que o gate G2 verifica na silver, como o resultado fica registado (a *atestação*) e como o
lakeFS recusa o merge. Os limiares marcados **(a calibrar)** são propostas: medem-se na semana
sintética e nas horas reais antes de ficarem definitivos.

## 1. O caminho de uma hora

    bronze ──► transform ──► upload da silver ──► G2 (lê o ficheiro) ──► upload da atestação ──► commit
                                                                                                     │
                      main ◄── merge ◄── HOOK pre-merge do lakeFS (lê a atestação e confere) ◄───────┘
                                              └─ falha: o merge é recusado (HTTP 412), o branch fica para autópsia

Duas camadas, de propósito:
- o **G2** (Python, no DAG) decide se a hora está boa e **escreve a prova**;
- o **hook** (Lua, dentro do lakeFS) **não confia no DAG**: só deixa entrar silver cuja prova existe,
  não é `fail` e se refere exatamente aos bytes que vão entrar.

## 2. As regras do G2 (por partição `dt`/`hour`, por ativo quando indicado)

| Regra | Verifica | `warn` | `blocker` |
|---|---|---|---|
| G2-01 completude | minutos da hora (por `ingest_ts`) em que o ativo teve pelo menos uma leitura GOOD, em 60 | < 98 % | **< 80 %** (a calibrar) |
| G2-02 unicidade | `bronze_event_id` únicos; `(asset, tag, ts_utc, server_ts)` repetido com valores diferentes (ADR-018: o `ts_utc` tem a precisão de 1 s do equipamento) | qualquer par | `bronze_event_id` repetido |
| G2-03 validade | % de linhas com a flag `out_of_range` | **> 0** | > 5 % |
| G2-04 qualidade | % de linhas com `quality != GOOD` | > 1 % | > 20 % |
| G2-05 frescura | a última linha do ativo está a menos de 5 min do fim da hora | atraso > 5 min | — |
| G2-06 *stuck* | variável analógica sem nenhuma linha na hora, num ativo com `device_clock` saudável | qualquer | — |
| G2-07 relógio | % de linhas com a flag `clock_skew` | > 0 | > 25 % |

Severidades: `blocker` bloqueia o merge; `warn` deixa passar e fica registado; `info` só regista.
Regra para escolher (do plano): se o dado errado chegar ao utilizador final e o levar a decidir
mal, é `blocker`.

**Porque a completude conta minutos e não linhas:** o gateway só escreve quando o valor muda, por isso
não há um número fixo de linhas por hora. E contar o `device_clock` deu um falso positivo na semana
sintética: no fim de um `clock_skew` o relógio recua, repete `source_ts` e valor, e o spool descarta
essas amostras (ponto fraco 8 da Fase 3). Minutos com dados é independente disso. Um sensor preso é
diferente: o ativo tem dados mas aquela variável não; é o G2-06.

**Resultado esperado dos incidentes** (é o DoD da fase):
`comms_gap` → G2-01 `blocker` → merge recusado · `spike` → G2-03 `warn` → merge passa ·
`sensor_stuck` → G2-06 `warn` · `clock_skew` → G2-07 `warn`.

## 3. A atestação

Um ficheiro por partição, no próprio branch, escrito pelo G2 depois do upload da silver:

    _gates/g2/dt=2026-11-07/hour=17.json

    {
      "gate": "G2", "version": 1,
      "status": "pass | warn | fail",            // fail se alguma regra blocker falhou
      "partition": "2026-11-07T17",
      "files": { "silver/dt=2026-11-07/hour=17/data.parquet": "<checksum do lakeFS>" },
      "rows": 43848,
      "rules": [ {"id": "G2-01", "severity": "pass|warn|blocker", "asset": "...", "value": 1.0, "threshold": 0.8, "detail": "..."} ],
      "summary": "texto curto para o humano"
    }

Um ficheiro por hora (e não um só) porque duas horas a correr ao mesmo tempo escreveriam o mesmo
ficheiro e o segundo merge teria conflito.

## 4. O hook (`quality/hooks/*.lua`, instalado como `_lakefs_actions/*.yaml`)

**`g2_pre_merge`** (evento `pre-merge`, destino `main`): para cada ficheiro `silver/` que o merge
acrescenta ou altera, exige a atestação dessa partição, no `source_ref` do merge, e verifica:
1. a atestação existe e é do gate `G2`;
2. `status` não é `fail`;
3. o `checksum` do ficheiro no branch é igual ao atestado (se mudou depois do G2, é recusado).
Se o merge não toca em `silver/` (por exemplo o bronze do writer), o hook não se aplica.

**`protect_actions`** (evento `pre-commit`): recusa qualquer commit que altere `_lakefs_actions/`.
Medido no protótipo: o lakeFS lê as ações **do branch de origem**, por isso um branch podia apagar o
hook e fazer merge sem gate. Esta proteção fecha isso. O instalador é a exceção, marcando o commit
com o metadado `gates_installer=true`.

**Limite conhecido:** quem tiver permissões de administrador e souber do metadado pode alterar os
hooks. O objetivo é impedir o acidente e obrigar a um ato deliberado, não defender de um atacante
(o RBAC fino do lakeFS é só da edição Enterprise).

## 4.1 O que o protótipo provou (lakeFS 1.86.0 open source, repositório descartável)

| Caso | Resultado |
|---|---|
| merge só de bronze (G2 não se aplica) | passa |
| silver sem atestação | **recusado** |
| silver + atestação `pass` / `warn` | passa |
| silver + atestação `fail` | **recusado** |
| atestação com checksum diferente / silver alterado depois de atestado | **recusado** |
| branch que apaga o ficheiro do hook | **recusado no commit** (graças ao `protect_actions`) |

## 5. Critérios de verificação da fase
1. injetar `comms_gap` numa hora real: G2 dá `fail`, o merge é recusado com mensagem do hook, e o `main` não muda;
2. injetar `spike`: G2 dá `warn`, o merge passa;
3. mexer na silver depois de atestada: o hook recusa;
4. as horas normais continuam a passar sem alterações ao que já corre (o DAG `silver_hourly`);
5. cada decisão (com regras e valores) fica consultável (no OpenMetadata, na etapa final da fase).
