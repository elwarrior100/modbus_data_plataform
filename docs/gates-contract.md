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
| G2-06 *stuck* | variável analógica sem nenhuma linha na hora, num ativo com `device_clock` saudável; um sinal de processo (`moves_with_run`) só conta se outro sinal de processo do ativo mexeu na hora | qualquer | — |
| G2-07 relógio | % de linhas com a flag `clock_skew` | > 0 | > 25 % |

Severidades: `blocker` bloqueia o merge; `warn` deixa passar e fica registado; `info` só regista.
Regra para escolher (do plano): se o dado errado chegar ao utilizador final e o levar a decidir
mal, é `blocker`.

**Porque a completude conta minutos e não linhas:** o gateway só escreve quando o valor muda, por isso
não há um número fixo de linhas por hora. E contar o `device_clock` deu um falso positivo na semana
sintética: no fim de um `clock_skew` o relógio recua, repete `source_ts` e valor, e o spool descarta
essas amostras (ponto fraco 8 da Fase 3). Minutos com dados é independente disso. Um sensor preso é
diferente: o ativo tem dados mas aquela variável não; é o G2-06.

**Ativo parado não é sensor preso (ADR-019, 2026-11-22).** Em `FAULT`, `STOPPED` ou `MAINTENANCE` os sinais de processo
(caudais, correntes, velocidade, binário, vibrações, potências; `moves_with_run: true` no catálogo) ficam constantes e o
gateway deixa de os escrever. Se nenhum deles teve linhas na hora, o ativo esteve parado a hora toda e o G2-06 não avisa
desses sinais; as temperaturas, que continuam com ruído a arrefecer, continuam a ser verificadas. Se pelo menos um mexeu,
a máquina rodou e um sinal de processo calado continua a ser suspeito. Limite aceite: se TODOS os sinais de processo de um
ativo prenderem ao mesmo tempo com a máquina a rodar a hora toda, não se deteta (seria quase sempre falta de comunicação,
que o G2-01 apanha). O G2-01 não muda: o `device_clock` continua a contar minutos com dados num ativo parado.

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
      "summary": "texto curto para o humano",
      "bronze_commit": "<ID do commit do lakeFS de onde a silver leu o bronze>"
    }

**`bronze_commit` (desde 2026-11-23, ADR-019 D8, T1).** O `run_hour` resolve o `main` para um ID de commit **antes** de
listar o bronze e lê a lista e os ficheiros desse commit fixo (o `main` avança enquanto se lê). O ID fica aqui e nos
metadados do commit da silver (`bronze_commit`), e o gold copia-o para o manifest da release: é assim que se responde a
"que commit de bronze originou esta release". O hook não o lê (não muda). Ao repetir uma hora, se a silver sai igual
(mesmo checksum) fica o commit da primeira prova: a mesma silver só vem do mesmo bronze, e repetir não cria commits.
As provas anteriores a esta data não têm o campo (o manifest diz "desconhecido"; ou reprocessa-se a hora, dentro dos 15
dias de bronze).

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

**Uma só ação por evento (2026-11-23).** Os hooks dentro da mesma ação correm um a seguir ao outro; ações diferentes no
mesmo evento correm **em paralelo**. Com o lakeFS 1.86, dois hooks Lua em paralelo que chamam `lakefs.diff_refs`
derrubam o servidor (`fatal error: concurrent map writes`, em `pkg/logging`): visto no cluster local com o G2 e o G3 em
ações separadas no `pre-merge` (6 reinícios do lakeFS; o `industrial-lake` não foi afetado). Por isso o G3 entra como
segundo hook da ação `g2` (`quality/install_hooks.py`), e um teste (`test_hooks_render.py`) recusa duas ações no mesmo
evento.

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

## 6. O gate G3: a coerência do gold de um dia (Fase 6, ADR-019 D6)

**Implementado no disco (2026-11-23), por rever; os hooks ainda NÃO estão instalados em nenhum lakeFS.**

O mesmo desenho do G2: o **G3** (Python, `pipelines/src/gold/g3.py`) decide e escreve a prova; o **hook**
(`quality/hooks/g3_pre_merge.lua`) não confia no DAG e confere-a no merge. A unidade é o **dia UTC** (`dt`).

    gold/<tabela>/dt=D/data.parquet (9 tabelas) ──► G3 (lê o gold E a silver) ──► _gates/g3/dt=D.json ──► commit
                                                                                                          │
                      main ◄── merge ◄── HOOK g3_pre_merge (atestação, sem fail, checksum de cada ficheiro) ◄─┘

### 6.1 As regras

| Regra | Verifica | `warn` | `blocker` |
|---|---|---|---|
| G3-01 reconciliação | soma das `samples` do `fact_tag_hour` = linhas da silver com o instante do evento em D (`ts_utc`; `server_ts` com `clock_skew`), por ativo e tag, contadas pelo G3 diretamente na silver; e o `fact_tag_minute` soma o mesmo que o `fact_tag_hour` | — | qualquer diferença |
| G3-02 buracos | cada ativo tem as 24 horas no `fact_asset_hour`, e cada tag no `fact_tag_hour` (uma hora sem dados aparece com cobertura 0, nunca falta a linha) | — | série sem as 24 horas |
| G3-03 física | cada hora soma 3600 s (estados + sem dados; tempo planeado + paragens planeadas); energia, volume e contagens ≥ 0; nos KPIs: disponibilidade e cobertura em [0, 1], kWh/m³ em [0,05; 2], rendimento em ]0, 1], COP em ]0, 12] **(a calibrar)** | — | fora |
| G3-04 cobertura | horas de D sem silver no `main` (recusadas pelo G2, ou que não correram) | qualquer | > 6 h **(a calibrar)** |
| G3-05 volume | amostras do dia contra a média dos dias anteriores que estão no gold (até 7) | ±20 % | — (`info` sem histórico) |
| G3-06 deriva | PSI das médias horárias de cada tag analógica contra os dias anteriores (decis da referência) | PSI > 0,25 | — (`info` com menos de 3 dias de histórico) |
| G3-07 estado desconhecido | um ativo com o dia inteiro "sem dados" no `fact_asset_hour` (nenhum segundo com estado), mas com leituras GOOD no `fact_tag_hour` — quase sempre falta o estado inicial (ADR-024) | qualquer | — |

O rendimento > 1 e o COP > 12 não estavam no ADR: são impossíveis fisicamente (um rendimento acima de 1 cria energia),
por isso entraram no G3-03 como bloqueio. O PSI fica em aviso de propósito: com o simulador, uma deriva é muitas vezes
um cenário (`drift`) e a Fase 8 precisa desses dados.

### 6.2 A atestação

    _gates/g3/dt=2026-11-01.json

    {
      "gate": "G3", "version": 1, "status": "pass | warn | fail", "partition": "2026-11-01",
      "files": { "gold/fact_asset_hour/dt=2026-11-01/data.parquet": "<checksum do lakeFS>", "...": "(as 9 tabelas)" },
      "rows": 1123786,                                // amostras da silver no dia
      "rules": [ {"id": "G3-05", "severity": "info", "value": 0, "threshold": 0.2, "detail": "...", "asset": null} ],
      "summary": "texto curto para o humano",
      "gold_version": "1",
      "inputs": {                                      // de onde veio o gold: o manifest da release copia isto
        "silver_commit": "<commit do main lido pelo gold>",
        "silver_partitions": { "silver/dt=2026-10-31/hour=23/data.parquet":
                               {"checksum": "...", "g2": "pass", "bronze_commit": "<do G2> | desconhecido (...)"} }
      }
    }

Todas as tabelas gold têm partição por dia, as `dim_*` incluídas (o ADR-019 D4 propunha-as sem partição): assim cada
ficheiro gold pertence a uma só atestação e o hook confere-o exatamente como o do G2.

### 6.3 Os hooks do gold (`quality/hooks/`, em `GOLD_ACTIONS` do `install_hooks.py`, ainda não instalados)

| Hook | Evento | Recusa |
|---|---|---|
| `g3_pre_merge` | `pre-merge` para o `main` | ficheiro `gold/` sem atestação do dia, com atestação `fail`, ou com checksum diferente do atestado |
| `release_pre_create_tag` | `pre-create-tag` | tag `gold-v*` sobre um commit sem `_releases/<tag>/manifest.json`, ou cujo dia não tem G3, tem G3 `fail`, ou cujos ficheiros não são os atestados |
| `release_pre_delete_tag` | `pre-delete-tag` | apagar uma tag `gold-v*` (no lakeFS uma tag apaga-se; sem isto, "imutável" seria só uma promessa) |

**A provar num repositório descartável** antes de instalar (`tests/test_gates_hook_gold.py`, marcado `lakefs`): os
campos `action.tag_id`/`action.commit_id` dos eventos de tag no lakeFS 1.86, e se o nome da tag aceita `+`.

## 7. Releases e o `LATEST_STABLE` (ADR-019 D7 e D9; 2026-11-23)
- **Tag** `gold-vMAJOR.MINOR.PATCH_AAAAMMDDHHmm` (com `_`: um `+` parte o `get_object` dos hooks Lua). O hook
  `release_pre_create_tag` exige, no commit da tag, o manifest `_releases/<tag>/manifest.json` e, para cada dia, a
  atestação G3 sem `fail` com os mesmos checksums. O `release_pre_delete_tag` recusa apagar uma `gold-v*`.
- **Ponteiro** `_releases/LATEST_STABLE` (JSON com `release`): só muda por merge, e o hook `release_pointer_pre_merge`
  (3.º hook da ação `g2`) só aceita uma release que existe, com manifest e G3 sem `fail`. O código só o avança sozinho
  com G3 `pass`; com `warn` avança-se à mão (`python -m gold.release point <tag>`). Rollback:
  `python -m gold.release rollback` (para a release aprovada anterior) ou `--to <tag>`.
