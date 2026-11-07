# Guia: perceber tudo o que fizemos

Lê isto com as imagens `docs/img-0*.svg` abertas ao lado. Cada secção diz que imagem ver.
Se uma palavra for nova, está explicada no glossário (secção 9).

---

## 1. O projeto numa frase

Uma **fábrica simulada** produz leituras de sensores (pressão, temperatura, vibração…).
Essas leituras são **traduzidas, guardadas, limpas e analisadas**, até acabarem num
dashboard e num modelo que prevê avarias. Tudo corre **no teu Mac**, sem a nuvem.

O trabalho está dividido em fases (ver `docs/PLAN.md`):

| Fase | O que é | Estado |
|---|---|---|
| 0 | A base: Kubernetes, base de dados, armazenamento, lakeFS | ✅ |
| 1 | O simulador Modbus (as máquinas) | ✅ |
| 2 | O gateway OPC UA (o tradutor) | ✅ |
| (extra) | Argo CD: o git passa a mandar no cluster | ✅ |
| 3 | Ingestão: ir buscar os dados e guardá-los sem perder nada | **a seguir** |
| 4 a 8 | Limpeza, qualidade, versões, dashboard, machine learning | por fazer |

👉 **Imagem 2**: o caminho dos dados, com o que está feito a verde.

---

## 2. Onde corre cada coisa: três "casas"

👉 **Imagem 1.**

Há três camadas, uma dentro da outra:

1. **O teu Mac**: onde estão o código, o git, o terminal e eu (Claude Code).
2. **A VM Lima**: um computador Linux *dentro* do Mac, com 8 CPUs, 16 GB de RAM e 150 GB de
   disco. Existe porque o Kubernetes precisa de Linux. Se a desligares, o cluster desliga.
3. **O k3s**: o Kubernetes, a correr dentro da VM. É um "gestor": tu dizes-lhe *o que
   queres a correr* e ele mantém isso vivo, reiniciando o que cair.

Dentro do k3s, os serviços estão arrumados em "pastas" chamadas **namespaces**:

| Namespace | Serviços | Para que servem |
|---|---|---|
| `data` | PostgreSQL, SeaweedFS, lakeFS | guardar dados e metadados |
| `ot` | `ot-edge` (simulador + gateway) | a fábrica simulada |
| `argocd` | Argo CD | manter o cluster igual ao git |

### O que cada serviço de `data` faz (e como se ligam)

- **PostgreSQL**: uma base de dados. Por agora só guarda os **metadados do lakeFS** (quem
  fez que commit, em que ramo). Mais tarde também servirá o Airflow e os dashboards.
- **SeaweedFS**: guarda os **ficheiros a sério**, como se fosse uma "pen" de rede (fala o
  protocolo S3, o mesmo da Amazon). Tem um único "balde" (bucket) chamado `lake`.
- **lakeFS**: fica *por cima* dos dois e dá aos dados superpoderes de git: **ramos,
  commits e etiquetas**. Os ficheiros ficam no SeaweedFS, o historial no PostgreSQL.

Porquê tanta camada? Para podermos **testar dados novos num ramo sem estragar os bons**, e só
juntá-los aos bons quando passarem as verificações. É a ideia central do projeto.

### O que está dentro do `ot-edge`

Um único contentor com **três programas**:

1. **socat**: cria um "cabo RS-232 virtual" com duas pontas (`/tmp/ttyV0` e `/tmp/ttyV1`).
2. **O simulador**: uma ponta do cabo. Faz-se passar por 3 máquinas (bomba, motor, chiller) e
   responde em **Modbus**, a "língua" antiga das máquinas industriais.
3. **O gateway**: a outra ponta. Pergunta ao simulador em Modbus e **traduz para OPC UA**, uma
   língua moderna, com unidades, intervalos e estados de qualidade.

Estão no mesmo contentor porque um cabo virtual só existe dentro do contentor que o criou.

---

## 3. A tradução Modbus → OPC UA, resumida

| O Modbus dá | O gateway entrega |
|---|---|
| um inteiro cru (`507`) | `5.07` bar, com tipo, unidade (`bar`) e intervalo (0 a 16) |
| silêncio quando a máquina falha | estado `BadNoCommunication`, **sem valor** |
| nenhum relógio | `SourceTimestamp` (do relógio da máquina) e `ServerTimestamp` |

A regra mais importante: **uma falha nunca se apresenta como valor bom**. Quando a máquina
não responde, o valor desaparece em vez de ficar o último número (que seria uma mentira).
Detalhes: `docs/opcua-address-space.md` e `docs/register-map.md`.

---

## 4. O git, explicado devagar

👉 **Imagens 3 e 4.**

O **git** é um caderno que guarda **fotografias** do projeto ao longo do tempo. Cada
fotografia chama-se **commit**, e tem uma mensagem e uma data. Podes sempre voltar atrás.

Os teus ficheiros podem estar em **três sítios** (imagem 3):

1. **A pasta de trabalho**: os ficheiros que vês e editas.
2. **O historial local**: os commits, guardados no teu Mac. Fazes um com `git commit`.
3. **O GitHub**: uma cópia na internet. Envias para lá com `git push`.

Um **ramo** (*branch*) é só uma etiqueta que aponta para um commit. Temos dois, `main` e
`test`, e neste momento apontam **ambos para o mesmo commit** (`8403ce2`), tal como as
cópias no GitHub (`origin/main` e `origin/test`). Está tudo em dia.

### O que fizemos no git (imagem 4)

15 commits, em ordem:

- **Plano (2)**: o `.gitignore` e o plano do projeto.
- **Fase 0 (10)**: regras de trabalho, VM, Postgres, SeaweedFS, lakeFS, o spike e os testes.
- **Fase 1 (1)**: o simulador. **Fase 2 (1)**: o gateway. **Argo CD (1)**.

As Fases 1 e 2 têm testes automáticos (46 para o simulador e 22 para o gateway), que
passaram antes de os commits serem feitos; a Fase 0 é infraestrutura e foi verificada com
scripts de fumo. No total o projeto tem **85 ficheiros**, ~1 150 linhas de código Python e
~940 linhas de testes.

Comandos que podes usar à vontade (só leem, não estragam nada):

```
git log --oneline          # a lista de commits
git status                 # o que mudou desde o último commit
git diff                   # o que mudou, linha a linha
git show 8403ce2           # o que um commit concreto alterou
```

---

## 5. Como uma alteração chega ao cluster (GitOps)

👉 **Imagem 5.**

Antes do Argo CD, eu aplicava as alterações ao cluster com `kubectl apply`. Agora **o git
manda**:

1. Alteras um ficheiro.
2. `git commit` guarda-o.
3. `git push` envia-o para o GitHub.
4. O **Argo CD** consulta o GitHub de 3 em 3 minutos e vê o commit novo.
5. Aplica a diferença ao cluster. **Se alguém mexer à mão no cluster, o Argo desfaz** (isto
   chama-se *self-heal*; testámos: desfez em 4 segundos).

Vantagens: o git é o **registo fiel** do que corre; podes voltar atrás com `git revert`; e
vê-se na interface do Argo se o cluster está "em dia" (*Synced*).

### O que NÃO está no git (de propósito)

- **Palavras-passe**: ficam só no cluster, criadas por `bootstrap-secrets.sh`. Nunca vão
  para o GitHub.
- **As imagens** (o código empacotado): são construídas à mão com `build-image.sh`. Ainda não
  temos CI (automação), fica para a Fase 9.
- **Os dados**: vivem no lakeFS/SeaweedFS, nunca no git.

---

### Como o Argo funciona por dentro

👉 **Imagem 6.**

O Argo CD é um **comparador que nunca descansa**. Em cada ciclo faz três coisas:

1. **Lê o que devia estar** (o `repo-server` vai ao GitHub e prepara os manifestos).
2. **Vê o que está** (o `application-controller` pergunta ao cluster).
3. **Compara.** Se são iguais, o estado é `Synced` e não faz nada. Se são diferentes, é
   `OutOfSync`, e, como as nossas aplicações são automáticas, **aplica a diferença**.

O que ele vigia está escrito numa "Application", que é um objeto do próprio cluster. A nossa
aplicação `data` diz, em resumo:

    repositório : git@github.com:elwarrior100/modbus_data_plataform.git
    ramo        : main
    pasta       : platform/k8s            (só os ficheiros do Postgres, SeaweedFS e lakeFS)
    destino     : o nosso cluster, namespace data
    automático  : selfHeal=true (desfaz alterações à mão)   prune=false (nunca apaga)

Repara no `prune=false`: se apagares um ficheiro do git, o Argo **não** apaga o serviço
correspondente. É de propósito, para uma base de dados não desaparecer por engano.

Temos quatro aplicações, organizadas em **"app-of-apps"**: a `root` vigia a pasta
`platform/argocd/apps/`, e cada ficheiro lá dentro (`namespaces`, `data`, `ot`) é outra
aplicação. Assim, para o Argo gerir uma coisa nova, basta acrescentar um ficheiro.

Duas palavras que vês na interface:

| Estado | Pergunta a que responde | Valores |
|---|---|---|
| **Sync** | o cluster é igual ao git? | `Synced` ou `OutOfSync` |
| **Health** | o que corre está vivo e saudável? | `Healthy`, `Progressing`, `Degraded` |

Podes estar `Synced` (igual ao git) mas `Degraded` (o git descreve algo que não funciona).
O Argo garante que o cluster **é igual ao git**, não que o git **esteja certo**.

---

## 6. Como ver tudo (as interfaces)

```
./platform/scripts/ui.sh          # abre os túneis para todas as interfaces
./platform/scripts/ui.sh --open   # e abre-as no browser
./platform/scripts/ui.sh --check  # só testa se respondem
k9s                               # o cluster no terminal (precisa do KUBECONFIG)
```

| Interface | Endereço | O que mostra |
|---|---|---|
| Argo CD | http://localhost:8081 | aplicações, se estão em dia com o git |
| lakeFS | http://localhost:8082 | ramos, commits e etiquetas dos dados |
| SeaweedFS | http://localhost:8888 | os ficheiros guardados |
| Simulador | http://localhost:8083/state | os valores atuais das 3 máquinas |
| OPC UA | `opc.tcp://localhost:4840/ot-gateway` | abrir com o UaExpert |

## 6b. Como usar o k9s (o cluster no terminal)

O `k9s` é uma "janela" dentro do terminal que mostra o cluster em tempo real. Ao contrário
do `kubectl`, não tens de decorar comandos: navegas com o teclado.

### Como ligar (tens de o fazer em cada terminal novo)

O `k9s` precisa de saber **onde está o cluster**. Tens duas formas:

```
# Forma 1: dizer ao terminal onde está o cluster (e depois usas k9s e kubectl à vontade)
export KUBECONFIG=~/.lima/industrial/copied-from-guest/kubeconfig.yaml
k9s

# Forma 2: dizer só ao k9s, nesse comando
k9s --kubeconfig ~/.lima/industrial/copied-from-guest/kubeconfig.yaml
```

Antes, confirma que a VM está ligada: `limactl list` deve mostrar `Running`. Se estiver
`Stopped`, corre `limactl start industrial`.

**Recomendo abrir sempre em modo só de leitura enquanto estás a aprender**, para nunca
apagares nada por engano:

```
k9s --readonly
```

### O que vês no ecrã

    ┌ topo ─────────────────────────────────────────────────────────┐
    │ cluster, versão, uso de CPU e RAM + as teclas disponíveis     │
    ├ meio ─────────────────────────────────────────────────────────┤
    │ a lista (por omissão, os pods), com estado, restarts e idade  │
    ├ fundo ────────────────────────────────────────────────────────┤
    │ o caminho onde estás (ex.: pods > ot) para saberes voltar     │
    └───────────────────────────────────────────────────────────────┘

### As teclas que precisas

| Tecla | O que faz |
|---|---|
| `:` e depois o nome | muda de vista: `:pods`, `:deploy`, `:svc`, `:ns` (namespaces), `:events` |
| `0` | mostra tudo, de todos os namespaces |
| `↑` `↓` (ou `j` `k`) | mover na lista |
| `Enter` | entrar no item selecionado (de um pod, vai aos contentores) |
| `l` | **ver os logs** do pod selecionado |
| `d` | descrever: eventos e configuração (o que o `kubectl describe` mostra) |
| `y` | ver o YAML do objeto |
| `/` | filtrar a lista por texto |
| `Esc` | voltar atrás |
| `?` | ajuda com todas as teclas |
| `:q` ou `Ctrl-C` | sair |

**Cuidado** (só fora do modo `--readonly`): `Ctrl-D` **apaga** o item selecionado. Num pod
gerido por um Deployment é inofensivo (é recriado), mas apagar outras coisas não.

### Três exercícios para treinares

**1. Ver o simulador a funcionar.** `k9s --readonly` → escreve `:ns` + Enter → seleciona `ot`
→ Enter → seleciona o pod `ot-edge-…` → prime `l`. Vês os logs: "Modbus TCP a escutar",
"Modbus RTU a escutar", "OPC UA a escutar". É o que o pod disse ao arrancar.

**2. Ver o Argo.** Escreve `:applications` + Enter. Aparecem as quatro aplicações, com
`Synced` e `Healthy`. Seleciona `data`, prime `d` e vês o que o Argo vigia.

**3. Ver o self-heal com os teus olhos** (este exercício precisa de **sair do modo de
leitura**: abre só `k9s`). Vai a `:deploy` no namespace `ot`, seleciona `ot-edge`, e prime `s`
(*scale*) para o pôr a 3 réplicas. Em cerca de 4 segundos o Argo volta a pô-lo a 1. Estás a
ver, em direto, o cluster a ser puxado para o git.

## 7. Se alguma coisa não funcionar

| Sintoma | Causa provável | Solução |
|---|---|---|
| `connection refused localhost:8080` | o terminal não conhece o cluster | `export KUBECONFIG=~/.lima/industrial/copied-from-guest/kubeconfig.yaml` |
| `kubectl` não responde de todo | a VM está desligada | `limactl start industrial` |
| pods em `Error`/`Unknown` logo após ligar a VM | ainda estão a arrancar | esperar 1 a 2 minutos |
| uma interface não abre | o túnel caiu | voltar a correr `ui.sh` |

**Verificação geral**, que diz se tudo está bem:

```
kubectl get pods -A                       # tudo deve estar Running
kubectl -n argocd get applications        # as 4 devem estar Synced e Healthy
./tests/phase1/run.sh                     # simulador, por Modbus TCP
./tests/phase2/run.sh                     # gateway OPC UA (inclui o RS-232)
./tests/spike/run.sh                      # lakeFS + DuckDB
```

---

## 8. As decisões, e onde estão

Cada escolha técnica importante tem um **ADR** (registo de decisão) em `docs/adr/`:

| ADR | Decisão |
|---|---|
| 000 | o spike que provou que o lakeFS e o DuckDB funcionam juntos |
| 001 | Kubernetes numa VM Lima (e não no Docker) |
| 002 | SeaweedFS em vez de MinIO (o MinIO deixou de publicar imagens) |
| 003 | o simulador Modbus é escrito por nós |
| 004 | o gateway OPC UA, no mesmo contentor que o simulador |
| 005 | Argo CD para GitOps |

---

## 9. Glossário

| Palavra | Quer dizer |
|---|---|
| **Contentor** | um programa empacotado com tudo o que precisa para correr |
| **Imagem** | o "molde" a partir do qual se cria um contentor |
| **Pod** | a unidade mínima do Kubernetes: um invólucro à volta de contentores |
| **Deployment / StatefulSet** | o objeto que mantém um pod vivo (o StatefulSet é para quem guarda dados) |
| **Service** | um nome estável para chegar a um pod, que muda de IP |
| **Secret** | onde se guardam palavras-passe no cluster (não vão para o git) |
| **Namespace** | uma "pasta" dentro do cluster |
| **Manifesto** | um ficheiro YAML que descreve o que queres a correr |
| **Modbus** | protocolo antigo das máquinas industriais: só números inteiros |
| **OPC UA** | protocolo moderno: valores com tipo, unidade, intervalo e qualidade |
| **RS-232** | o cabo série clássico; o nosso é virtual |
| **Parquet** | formato de ficheiro para tabelas, compacto e rápido |
| **Bucket** | um "balde" de ficheiros num armazenamento tipo S3 |
| **Branch / tag** | etiqueta móvel / fixa que aponta para um commit (no git e no lakeFS) |
| **GitOps** | o git é a verdade e o cluster segue-o |
| **ADR** | um documento curto que regista uma decisão e porquê |
