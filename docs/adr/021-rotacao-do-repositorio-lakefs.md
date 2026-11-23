# ADR-021: um repositório lakeFS por período (teto para os metadados e para a GC)

**Estado:** aceite (2026-11-23, escolhas na secção 5). Nenhum código ainda: o script e o ensaio vêm antes de 1-1-2027.
**Liga a:** ADR-015 (retenção e GC), ADR-016 (cópia em pacotes), ADR-019 (gold e releases), ADR-020 (crescimento do lake).

## 1. O problema, em simples

O lakeFS funciona como o git: cada lote do writer e cada hora de silver são um **commit** (~240 por dia), e cada
commit guarda para sempre o seu índice dos ficheiros (os metadados, em `_lakefs/` no SeaweedFS). Pode-se deitar fora
ficheiros antigos (a GC faz isso ao bronze com mais de 15 dias), mas **num repositório não há forma de apagar commits
antigos nem os seus índices**. Daí dois efeitos que nenhuma retenção resolve:

| O quê | Hoje | Ritmo | Quando dói |
|---|---|---|---|
| Metadados (`_lakefs/`) | 1,02 GB (Oracle, 22-11) | ~1,8 GB/ano com lotes de 15 min (ADR-020) | a cópia no Object Storage guarda-os ~2×: o pico chega aos 20 GB grátis ao fim de ~1 ano |
| Tempo da GC de domingo (lê **todos** os commits) | 11,5 min, 874 MiB (22-11) | ~88 mil commits/ano | estimativa linear: 1 h em ~7 meses, o prazo de 3 h em ~2 anos; se a GC deixar de acabar, o bronze apagado nunca é libertado |

A retenção da silver (3 meses, ADR-020) põe teto aos **dados**, mas não aos **metadados** nem à GC.

## 2. A proposta: começar um repositório novo a cada período

Como um caderno de contabilidade que se fecha no fim do ano e se abre outro só com os saldos:

1. **Pausa curta** (minutos): param o writer e as DAGs. O collector continua a guardar na spool, por isso não se perde
   nenhuma amostra; o writer recupera o atraso ao retomar (lotes até 20 000 linhas).
2. **Repositório novo** (ex.: `industrial-lake-2027`) com o seu próprio espaço no SeaweedFS (um bucket novo, `lake-2027`,
   para a remoção do antigo ser apagar um bucket, sem tocar no novo).
3. **Copia-se só o estado atual do `main`:** silver dos últimos 3 meses, gold todo, bronze dos últimos 15 dias, as
   atestações dos gates (`_gates/`, poucos KB) e os manifests das releases (`_releases/`). Num só commit: o caderno novo
   começa com uma linha. A cópia é dentro do SeaweedFS (~5 GB, minutos; €0).
4. **Instalam-se os hooks** (G2, G3, proteção das ações e das tags) e cria-se uma release de arranque (`gold-v…`) para o
   `LATEST_STABLE` apontar a algo que existe no repositório novo.
5. **Troca-se o nome** num só sítio da plataforma (uma variável `LAKEFS_REPOSITORY`, hoje escrita à mão em 4 sítios:
   writer, tarefas do Airflow, `silver-freshness`, `lakefs-gc`) e no bucket da cópia e do ensaio de restauro.
6. **Retoma-se** e confirma-se: reconciliação spool ↔ bronze sem faltas, uma hora de silver com G2, o ensaio de restauro.
7. **O repositório antigo:** exporta-se para o novo, como ficheiros, o que é preciso para a rastreabilidade (o registo
   dos commits do `main`, ~10 MB por ano, em `_archive/<ano>/commits.jsonl`) e depois **apaga-se** (ou fica só de
   leitura um período; escolha 2).

**Resultado:** os metadados e a GC nunca passam de um período de commits; o lake e a cópia estabilizam (~8–10 GB de lake,
~16–18 GB no pico da cópia) em vez de crescerem todos os anos. A GC de domingo fica sempre abaixo de ~1 h (estimativa).

**O que se perde:** voltar atrás no tempo, pelo lakeFS, para dentro de um período anterior, e fazer rollback do gold para
uma release anterior à rotação. Hoje isso já só funciona 15 dias para o bronze (a GC apaga os ficheiros), e as releases
antigas ficam descritas nos seus manifests (que dizem que commit de bronze originou cada uma, ADR-019 D8).

## 3. Alternativas

| Opção | Teto? | Custo | Nota |
|---|---|---|---|
| **R1 — rotação periódica (proposta)** | sim | um procedimento por período, com script e ensaio no cluster local | o único teto real com o lakeFS open source |
| R2 — não fazer nada e vigiar | não | — | a GC chega ao prazo em ~2 anos; o Object Storage passa os 20 GB |
| R3 — lotes de 30 min (ADR-020) | não | a silver só às :40 | metade do ritmo, adia ~1 ano |
| R4 — lakeFS Enterprise ou outro formato (Iceberg com expiração de snapshots) | sim | pago, ou reescrever o lake (decisão D3/D4) | fora do custo zero / do âmbito |

## 4. Riscos e como se reduzem
- **Procedimento raro (1–2 vezes por ano) = fácil de esquecer:** um script (`scripts/lakefs-rotate.sh`) com modo de
  ensaio, provado primeiro no cluster local, e uma secção no runbook. Um alarme no mês anterior a lembrar.
- **Apagar o repositório errado:** o antigo só se apaga depois de o novo passar a reconciliação e o ensaio de restauro,
  e com o nome escrito à mão (como o `lakefs-gc-sweep.sh`).
- **Algo que ainda use o nome antigo:** uma só variável na plataforma e um teste no CI que procura o nome escrito à mão.

## 5. Escolhas do Stéphane (2026-11-23)
1. **Período: anual**, a 1 de janeiro (`industrial-lake-<ano>`). Teto: ~1,8 GB de metadados e um ano de commits na GC.
2. **O repositório antigo: fica 30 dias só de leitura e depois apaga-se** (avaliado a pedido do Stéphane):
   - apagar logo seria irreversível no mesmo dia da operação mais delicada do ano; guardar um ano manteria dois
     repositórios no disco o ano inteiro, sem ganho, porque o essencial (manifests e registo dos commits) fica exportado;
   - durante os 30 dias: sem escritas, sem GC (nada se apaga lá dentro) e **fora da cópia para o Object Storage** (não
     pesa nos 20 GB); ocupa ~5 GB no disco da VM (64 GB livres), €0. Se a VM se perdesse nesse mês, perdia-se só o
     antigo, e o essencial já está no novo;
   - apaga-se ao fim de 30 dias só se o novo tiver passado a reconciliação, o ensaio de restauro e um mês de releases, e
     com o nome escrito à mão.
3. **Primeira rotação: 1 de janeiro de 2027.** O gold e as releases nascem antes, em `industrial-lake`, e passam na
   primeira rotação (por isso a cópia inclui `_releases/` e o gold todo, e cria-se uma release de arranque no novo).

**Por fazer antes de 1-1-2027:** `scripts/lakefs-rotate.sh` (com modo de ensaio), o nome do repositório numa só
variável da plataforma, um ensaio completo no cluster local, a secção do runbook e um lembrete no início de dezembro.

## 5.1 Custo (verificado a 2026-11-23)
- **A cópia para o repositório novo** é dentro do SeaweedFS, no disco da VM: €0 e nenhum pedido ao Object Storage.
- **O Object Storage na transição:** durante 9 dias (a regra de ciclo de vida da cópia) convivem os pacotes do antigo e o
  primeiro completo do novo. Com as contas do ADR-020 para janeiro de 2027: ~10 GB (antigo) + ~5 GB (novo) + ~2,3 GB
  (dumps) ≈ **17 GB** num dia normal; mas nos 2 dias a seguir a um completo semanal o antigo tem dois completos
  (≈ 24 GB, até ~€0,01). **Regra do procedimento:** nunca rodar nos 2 dias a seguir a um pacote completo do antigo (o
  script verifica-o no catálogo mais recente e recusa). Assim a transição fica sempre abaixo dos 20 GB.
- **Pedidos:** o primeiro completo do novo são poucos pedidos (um por parte de 3 GiB); os 30 dias só de leitura do antigo
  não fazem nenhum (fora da cópia e da GC).
- **Estado da conta no dia da decisão:** €0,0200 em novembro (tudo de 15-11), previsão €0; 137 pedidos em 30 h.

## 5.2 Revisão de custos e testes (2026-11-23, agente de verificação)
- **As contas da 5.1 são conservadoras:** os "~10 GB do antigo" vêm das contas "a 1 ano" do ADR-020. A 1-1-2027 ainda
  haverá pouca silver: o mais provável é a cópia do antigo ter 3–6 GB e a transição ficar abaixo dos 15 GB. O script
  mede o tamanho real antes de rodar e mostra-o.
- **Faltava no procedimento** (entra no `scripts/lakefs-rotate.sh` e no runbook):
  1. **Forçar um pacote completo no bucket novo.** O `lake-sync` só faz um completo quando o mais recente tem 7 dias ou
     mais; com um bucket novo faria um incremental gigante, com um catálogo que mistura chaves antigas e novas. O script
     arranca a primeira cópia do novo em modo completo, com um catálogo próprio do bucket novo.
  2. **O `lake-sync` e o ensaio de restauro (`lake-restore-drill`) passam para o bucket novo** no mesmo commit que o nome
     do repositório.
  3. **Os alarmes de 12 GB (cópia do lake) e de 15 GB (total)** podem disparar nos 9 dias da transição: o runbook diz que
     é esperado, e o script avisa antes.
  4. **A regra "não rodar nos 2 dias a seguir a um completo" contra a data fixa de 1 de janeiro:** se o completo semanal
     do antigo cair a 30 ou 31-12, roda-se na mesma a 1-1 e apagam-se à mão, depois de o novo ter o seu completo
     verificado, os pacotes do antigo que já não são precisos (poucos pedidos, com o nome escrito à mão, como o
     `lake-backup-prune.sh`). A alternativa é adiar a rotação 1–2 dias; escolhe-se no dia, com o script a mostrar os números.
- **Um efeito bom que não estava contado:** os dumps do PostgreSQL (`lakefs.dump`) também guardam os commits; encolhem
  quando o antigo for apagado.
- **Verificado:** nenhuma das mudanças de 22–23-11 trouxe custos (novembro €0,020030, tudo de 15-11); testes do commit
  6828291 todos verdes; o PATCH do OpenMetadata provado no local (mesma versão da Oracle).

## 6. Consequências (se aceite)
- Mais um procedimento de operação, com script, ensaio e runbook.
- O nome do repositório passa a ser uma variável única na plataforma.
- A rastreabilidade de uma release antiga passa a ser pelo manifest e pelo registo exportado, não pelo lakeFS.
