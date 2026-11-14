# ADR-008: Gates G2/G3 e imposição por hook do lakeFS

Estado: aceite (2026-11-10). Contrato: `docs/gates-contract.md`.

## Contexto
O plano pede que um gate que falha **impeça o merge** e que isso seja garantido pelo lakeFS, não por
convenção. É preciso decidir onde corre a verificação, como o lakeFS a impõe e onde se guardam os resultados.

## Decisões
| Decisão | Escolha | Alternativas | Razão |
|---|---|---|---|
| Mecanismo do hook | **Lua dentro do lakeFS** (testado na versão 1.86.0 open source) | webhook para um serviço nosso | sem serviço extra para manter; lê o branch com a identidade do merge; o protótipo bloqueou e deixou passar como esperado |
| Onde corre a lógica do gate | **Python no DAG** (DuckDB sobre o ficheiro) | tudo em Lua | Lua não tem motor SQL nem leitura de Parquet; o DAG já tem o ficheiro |
| Como o hook "confia" | **atestação por partição** com o *checksum* dos ficheiros validados | o hook recalcula tudo | o hook fica pequeno e rápido e prende o resultado ao conteúdo exato, não ao nome do branch |
| Um ficheiro de atestação | **um por hora** (`_gates/g2/dt=…/hour=….json`) | um só | execuções em paralelo escreveriam o mesmo ficheiro e dariam conflito no merge |
| Fuga do hook | **hook `pre-commit` protege `_lakefs_actions/`** | nada | medido: o lakeFS lê as ações do branch de origem; um branch podia apagar o hook |
| Completude | **minutos da hora com pelo menos uma leitura GOOD, por ativo** | linhas do `device_clock`; contar todas as linhas | o gateway só escreve ao mudar; o `device_clock` deu um falso positivo na calibração (recuo do relógio + dedup do spool) |
| Severidades | `blocker`/`warn`/`info` do plano | — | `comms_gap` bloqueia, `spike` avisa (DoD) |
| Resultados no catálogo | OpenMetadata, **no fim da fase** | primeiro | os gates e o hook são o núcleo; o catálogo é pesado e vem a consumir as atestações |

## Consequências
- Um merge de silver sem atestação válida falha com a mensagem do hook; o branch fica por juntar (autópsia).
- Quem alterar os hooks precisa de o fazer de propósito (metadado `gates_installer`).
- Os limiares são propostas e calibram-se com a semana sintética e com horas reais.
- O G2 só cobre a silver; o G3 (gold) só faz sentido na Fase 6, quando o gold existir.

## Descobertas do protótipo (para quem repetir)
- `lakefs.diff_refs`/`diff_branch`/`stat_object` devolvem `(estado HTTP, tabela ou JSON em texto)`.
- Os argumentos opcionais têm ordem diferente: `diff_refs(repo, esq, dir, after, prefix, delimiter, amount)`
  e `diff_branch(repo, branch, after, amount, prefix, delimiter)`.
- O nome da ação e do hook precisa de pelo menos 2 caracteres.
- O contexto do hook traz `source_ref` (commit da ponta do branch a juntar), `branch_id` (destino) e `merge_source`.

## Primeira corrida com dados reais (2026-11-11)
- **O G2 recusou as horas com buracos reais.** O Mac esteve em suspensão, a VM congelou e o collector ficou sem
  receber. Medido à parte do gate: a hora 10-11 21h tem 44 de 60 minutos com dados, a de 11-11 03h só 13 de 60;
  a de 10-11 22h tem 60 de 60 e entrou no `main`. As horas recusadas ficam no branch `silver-<dt>-<hh>` (retenção de 30 dias).
  É o DoD da fase (`comms_gap` recusado por completude), obtido por acidente com dados reais.
- **Hooks instalados** no `industrial-lake` real (commit "gates: instalar hooks G2"); o writer do bronze continua a fazer
  merges sem problema (o G2 só se aplica a merges que tocam em `silver/`).
- **Uma execução do DAG recusada fica "failed" no Airflow**, sem repetições (`AirflowFailException`). Para a voltar
  a correr depois de decidir (por exemplo, depois de os dados chegarem), faz-se *clear* da execução.
- **G2-02 (aviso)** dispara em quase todas as horas reais (1 a 186 pares `(ativo, tag, instante)` com valores
  diferentes, em ~40 mil linhas): dois valores no mesmo segundo. É só aviso; a rever se o ruído atrapalhar.

## Incidente de armazenamento (2026-11-09/11)
A VM Lima foi **morta, não desligada** (as paragens de 07-11 e 09-11 não têm sequência de encerramento no diário).
O SeaweedFS perdeu o fim do ficheiro de volume e uma *metarange* do lakeFS (o commit de 09-11 22:59:20, lote
`392969388b01ac59`) ficou ilegível: o lakeFS deixou de criar branches e o writer parou.
Reparação (decidida com o Stéphane): `hard_reset` do `main` para o último commit legível (78b9874878), reposição do mesmo
branch `etl-…` para esse commit e só depois apagado (o lakeFS lê os hooks a partir do commit da ponta), lote
reposto a `pending` no spool, e `reset_changes` aos ficheiros de hooks que um commit falhado deixou em *staging* no `main`.
Reconciliação final: 439 213 amostras no spool = 439 213 `event_id` distintos no lakeFS, 0 em falta, 0 duplicados.
**Prevenção:** parar a VM com `limactl stop industrial` antes de fechar o Mac ou a app; a rever `fsync` no SeaweedFS.
Imagens: `docs/img-21`, `img-22`, `img-23`.
- O *hard reset* não está no SDK Python (`PUT /api/v1/repositories/{repo}/branches/{branch}/hard_reset?ref=<commit>`
  na API direta, testado num repositório descartável antes do uso real).
- Um scheduler do Airflow fica em `CrashLoopBackOff` (`DetachedInstanceError`) se existir uma tarefa "órfã" marcada `running`
  de um pod que morreu com a VM; resolveu-se marcando essa tarefa e execução como `failed` na base de metadados.

## Segundo incidente de armazenamento e a causa de fundo (2026-11-11)
Depois de parar e arrancar a VM, o PostgreSQL, o lakeFS e o `gosu` falharam, e o commit mais recente do `main` (09:16:59)
voltou a ficar ilegível. Duas causas distintas:
1. **Atualização automática do kernel.** O Ubuntu da VM passou de `7.0.0-38` para `7.0.0-42` a 10-11 (atualizações
   automáticas) e só entrou em vigor neste reinício. No `-42` o AppArmor (perfil `cri-containerd.apparmor.d`) **recusa
   sinais entre processos do mesmo contentor** (`operation="signal"`, 270 recusas do postgres): o `gosu` ficou num ciclo
   de `sched_yield` e o PostgreSQL falhou com `could not signal for checkpoint: Permission denied`.
   Correção: `GRUB_DEFAULT` fixo no kernel `7.0.0-38` (cópia em `/etc/default/grub.bak-2026-11-11`) e
   `apt-mark hold` em `linux-image-7.0.0-38-generic`, `linux-modules-7.0.0-38-generic`, `linux-image-virtual`,
   `linux-virtual` e `linux-generic`. Para desfazer: `apt-mark unhold …` e repor `GRUB_DEFAULT=0`.
   A rever: voltar ao kernel novo quando o AppArmor/containerd o tratarem.
2. **`limactl stop` não é gracioso para os pods.** O k3s corre com `KillMode=process`: ao parar a VM os contentores não
   recebem SIGTERM e o PostgreSQL ficou "not properly shut down"; o SeaweedFS perdeu os últimos segundos (a metarange do
   último commit). Correção: `platform/scripts/vm-stop.sh` (drain do nó, `sync`, `limactl stop`) e `vm-start.sh`
   (`uncordon`). Por testar de ponta a ponta.
- O `hard_reset` do `main` para o último commit legível continua a ser a reparação quando isto acontece; exige sempre
  confirmação do Stéphane, porque reescreve a história do `main`.

## Prova ao vivo e teste dos scripts de VM (2026-11-11)

- **G2 ao vivo (hora 18 UTC):** `comms_gap` + `spike` injetados; `run_hour` levantou `GateFailed` (G2-01 blocker nos 3 ativos,
  G2-03 warn do spike). Merge forçado à mão: **412** do hook `pre-merge`, `main` intacto. Contagem independente no spool:
  CHILLER-01 15, MOTOR-01 34, PUMP-01 34 minutos com dados (igual ao G2). A hora tinha também um buraco não injetado
  (18:05–18:26, VM a arrancar), por isso os três ativos falharam.
- **Terceira reparação do `main`** (mesmo sintoma: último commit ilegível após paragem da VM): hard reset para o último commit
  bom + lote reposto a `pending`; reconciliação OK.
- **`vm-stop.sh` + `vm-start.sh` testados:** drain de 2 min 38 s, VM parada, arranque em 70 s, todos os pods `Running`,
  kernel 7.0.0-38, reconciliação OK (545 540 amostras, 0 em falta/a mais/duplicadas). O writer falha 1–2 vezes enquanto o
  lakeFS arranca e recupera sozinho.


## Limites do lakeFS open source descobertos em 2026-11-14

- **O `main` está protegido** (regra de branch do lakeFS): não aceita escritas nem commits diretos, só merges. O
  instalador dos hooks passou a fazer commit num branch temporário e merge.
- **Só existe um utilizador.** Com autenticação básica, o lakeFS open source só admite o admin (mais utilizadores exigem
  o servidor ACL à parte): as aplicações continuam com a chave de administrador. Testado: criar `app-writer` dá
  "Already exists" e o utilizador não aparece.
- **Um hook não sabe quem faz o commit.** O objeto `action` de um `pre-commit` traz o branch, a mensagem e os
  metadados, mas nenhum utilizador. Por isso a exceção `gates_installer=true` não pode ser ligada a uma identidade.
- **Consequência honesta:** o gate impede acidentes e escritas por fora do pipeline, mas não impede quem tenha a chave
  de administrador. Fechar isto exige o servidor ACL do lakeFS (ou a versão Enterprise), com uma chave por aplicação.
