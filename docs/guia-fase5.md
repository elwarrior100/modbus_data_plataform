# Guia da Fase 5: gates de qualidade e imposição no lakeFS

Para perceberes (e saberes explicar) o que foi feito, como se o tivesses feito tu. Nada disto está
commitado: é teu para rever. Contrato: `docs/gates-contract.md`. Decisões: `docs/adr/008-gates-e-imposicao.md`.

---

## 1. A ideia, em uma frase

Antes, o pipeline **verificava** a qualidade. Agora **não deixa passar** dados maus: o `lakeFS`
recusa o merge para o `main` se a hora de silver não tiver uma prova de que passou no gate.

Pensa num aeroporto: o **G2** é o controlo de segurança (decide quem pode passar e emite o cartão de
embarque); o **hook** é o porteiro da porta do avião, que confere o cartão e não confia em ninguém.

## 2. As peças

| Peça | O que é | Onde está |
|---|---|---|
| **G2** | 7 regras sobre a silver de uma hora; decide `pass`, `warn` ou `fail` | `pipelines/src/silver/g2.py` |
| **Atestação** | o "cartão de embarque": um JSON por hora com o resultado e o *checksum* do ficheiro validado | `_gates/g2/dt=…/hour=….json` (no lakeFS) |
| **Hook `pre-merge`** | programa em **Lua** que corre *dentro do lakeFS* antes de cada merge para o `main` | `quality/hooks/g2_pre_merge.lua` |
| **Hook `pre-commit`** | impede que um commit normal apague ou altere os hooks | `quality/hooks/protect_actions_pre_commit.lua` |
| **Instalador** | escreve os hooks no repositório (num commit marcado `gates_installer=true`) | `quality/install_hooks.py` |

👉 **Imagem 24** (`docs/img-24-duas-fechaduras.svg`): o caminho completo e porque há duas fechaduras.
👉 **Imagem 25** (`docs/img-25-prova-e-checksum.svg`): como a prova se prende ao ficheiro exato.

## 3. As regras do G2

| Regra | Pergunta | Aviso (`warn`) | Bloqueia (`blocker`) |
|---|---|---|---|
| G2-01 completude | em quantos dos 60 minutos o ativo teve dados? | < 98 % | **< 80 %** |
| G2-02 unicidade | `event_id` repetidos? mesmo (ativo, tag, instante) com valores diferentes? | pares diferentes | `event_id` repetido |
| G2-03 validade | % de linhas fora do intervalo físico | **qualquer** | > 5 % |
| G2-04 qualidade | % de linhas com qualidade que não é `GOOD` | > 1 % | > 20 % |
| G2-05 frescura | a última linha chegou perto do fim da hora? | atraso > 5 min | — |
| G2-06 sensor parado | variável analógica sem nenhuma linha, com o ativo saudável | qualquer | — |
| G2-07 relógio | % de linhas com o relógio do dispositivo desalinhado | > 0 | > 50 % |

Um gate **não corrige**: assinala. `blocker` impede o merge; `warn` deixa passar e fica registado.

**Porque a completude conta minutos:** o gateway só escreve quando o valor muda, por isso não há um
número fixo de linhas por hora. Medi primeiro pelas linhas do relógio do dispositivo e deu um falso
positivo na semana sintética (o relógio recuou no fim de um `clock_skew` e o spool descartou amostras
repetidas). "Minutos com pelo menos uma leitura" é imune a isso.

## 4. Como foi feito, por ordem (para o repetires)

1. **Contrato e ADR primeiro.** Decidir as regras e as severidades com o papel à frente, antes do código.
2. **Descobrir se o lakeFS open source faz isto.** Em vez de assumir, fiz um protótipo num repositório
   descartável: os hooks Lua funcionam na versão 1.86.0 e um `pre-merge` que falha aborta o merge.
3. **Descobrir a fuga.** O lakeFS lê os hooks **do branch de origem**: um branch podia apagar o hook e
   fazer merge sem gate. O protótipo mostrou-o, e a proteção `pre-commit` fechou-o.
4. **Calibrar com dados.** Corri o G2 nas 169 horas da semana sintética: 163 passaram sem ruído e os 5
   incidentes deram o resultado do contrato. Esta calibração apanhou o falso positivo da completude.
5. **Ligar ao pipeline.** `run_hour` corre o G2, escreve a atestação com o *checksum* do ficheiro e só
   depois tenta o merge. Se o G2 der `fail`, levanta `GateFailed` (o Airflow não repete).
6. **Testar contra o lakeFS real** num repositório descartável: o hook aceita a atestação que o Python
   gera, recusa uma hora bloqueada mesmo contornando o Python, recusa prova ausente ou adulterada, e
   recusa apagar o hook.
7. **Instalar no repositório real** e ver o comportamento com dados verdadeiros.

## 5. O que aprendi pelo caminho (úteis a quem repetir)

- As funções Lua do lakeFS devolvem `(estado HTTP, tabela ou JSON em texto)` e os argumentos opcionais
  têm ordem diferente entre `diff_refs` e `diff_branch`.
- Em Lua do lakeFS, `string:match` não se comportou como esperado; partir o caminho com a biblioteca
  `strings` funcionou.
- O nome da ação e do hook precisa de pelo menos 2 caracteres.
- Para apagar um branch o lakeFS lê os hooks a partir do commit da sua ponta; se esse commit estiver
  ilegível, é preciso repor o branch antes de o apagar.

## 6. Incidente real pelo caminho (imagens 21, 22 e 23)

A VM foi morta (não desligada) e perdeu o fim de um ficheiro do SeaweedFS; o último commit do `main`
ficou ilegível e o writer parou. Reparou-se com um *hard reset* para o último commit bom e reposição do
lote no spool. Reconciliação final: 439 213 amostras iguais nos dois lados, 0 em falta, 0 duplicadas.
**Regra prática: `limactl stop industrial` antes de fechar o Mac.**

Foi o próprio G2 que mostrou, depois, que a VM dormia com o Mac: recusou as horas em que o collector
ficou congelado (por exemplo 13 de 60 minutos com dados).

## 7. Prova ao vivo dos incidentes (DoD da fase)

Corrida real de 2026-11-11, hora 18 UTC (disparada à mão às 19:02 com `run_hour`, igual ao DAG):

| Passo | Resultado |
|---|---|
| Incidentes injetados | `comms_gap` no CHILLER-01 (18:35–18:55) e `spike` no PUMP-01 (`outlet_pressure`=20, 18:49) |
| `run_hour` | `GateFailed`: CHILLER-01 15/60 minutos com dados (25 %), MOTOR-01 e PUMP-01 34/60 (57 %): três `blocker` G2-01 |
| Branch | `silver-2026-11-11-18` fica por juntar, com atestação `status: fail` |
| Merge **forçado** à mão (a contornar o Python) | **412** do lakeFS: `pre-merge hook aborted ... action g2`; o `main` ficou no mesmo commit |
| Hora com aviso (`warn`) | 3 atestações `warn` já fundidas no `main` (ex.: `dt=2026-11-09/hour=22`, G2-02): `warn` passa e fica registado |

**Contagem independente** (SQL direto ao spool, minutos distintos com leitura `GOOD` na hora 18): CHILLER-01 15,
MOTOR-01 34, PUMP-01 34. Igual ao G2.

**Honestidade:** a hora saiu com um buraco que **não** foi injetado: não há dados entre 18:05 e 18:26 em
nenhum ativo (o Mac/VM estava a arrancar). É por isso que MOTOR-01 e PUMP-01 também falharam. O `comms_gap`
fica provado (15 = 34 − 19 minutos de silêncio), mas a hora não isola o caso. O `spike` também ficou registado
na atestação: G2-03 `warn` (0,01 % das linhas fora do intervalo), tal como o contrato prevê (`spike` só avisa). Além disso
G2-07 `warn` (0,10 % de relógio desalinhado) e G2-02 `warn`. O resumo do log mostra só os 4 primeiros achados.

## 8. Perguntas para teres a certeza

1. Porque é que o hook não se limita a confiar no resultado do DAG?
2. O que impede alguém de validar um ficheiro bom e trocá-lo por outro antes do merge?
3. Porque é que a completude conta minutos com dados em vez de contar linhas?
4. Porque é que `comms_gap` é `blocker` e `spike` é só `warn`?
5. O que acontece a uma hora recusada, e como se volta a tentar?
6. Que limite tem esta proteção? (Quem tem permissões de administrador e conhece o metadado pode alterar os hooks:
   serve para impedir o acidente, não um atacante.)
