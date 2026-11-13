# ADR-010: separar em repositório de plataforma (GitOps) + um repositório por projeto

**Estado:** proposto (2026-11-12) · **Liga a:** ADR-005 (GitOps/Argo CD), docs/PLAN.md §9 (Oracle) e §12 (estrutura)
👉 Imagem: `docs/img-26-plataforma-e-projetos.svg`

## Contexto
Hoje está tudo num repositório: código (`ot`, `ingest`, `pipelines`, `quality`), manifests k8s, valores Helm,
scripts de instalação e o Terraform da Oracle. As imagens são construídas no Mac (`localhost/...`) e só existem
dentro da VM local. Isto não é um monolito (as peças são independentes), mas mistura **código** com **configuração
de ambiente**, e não serve para um segundo ambiente (Oracle) nem para um segundo projeto.

O objetivo do Stéphane: um repositório de infraestrutura onde só se aponta a imagem nova, e o Argo CD em cada
cluster (local e Oracle) aplica; e que esse repositório **sirva também para projetos futuros com outros DAGs**.

## Decisão

### 1. Dois tipos de repositório
| Repositório | Conteúdo | Muda quando |
|---|---|---|
| **`gitops`** (plataforma, genérico) | Terraform, plataforma partilhada, lista de projetos, ajustes por ambiente | muda a configuração ou a versão de uma imagem |
| **um por projeto** (o atual é o primeiro: `modbus_data_plataform`) | código, DAGs, Dockerfiles, testes, docs, CI que publica imagens | se programa |

Não partir mais (um repositório por `ot`/`ingest`/`pipelines`): partilham contratos (envelope, catálogo, bronze) e os
testes atravessam-nos (semana sintética). Para uma pessoa só, versões coordenadas entre repositórios são custo sem ganho.

### 2. Estrutura do `gitops`
```
gitops/
├── terraform/oracle/          a máquina (VM, rede)
├── platform/                  partilhado por TODOS os projetos
│   ├── argocd/                o Argo CD e o ApplicationSet dos projetos
│   ├── airflow/               UM Airflow para todos
│   └── postgres/ lakefs/ seaweedfs/ openmetadata/
├── projects/
│   ├── modbus/                só deste projeto: ot, ingest, origem dos DAGs, imagem
│   └── <outro>/               projeto novo = pasta nova + commit
└── envs/
    ├── local/                 Mac: mais recursos
    └── oracle/                2 CPU / 12 GB, tudo em série
```
O Argo CD usa um **ApplicationSet**: cada pasta em `projects/` vira uma aplicação. Juntar um projeto não mexe na plataforma.

### 3. Fluxo de uma alteração
1. Push no repositório do projeto → CI (GitHub Actions) corre os testes e constrói a imagem **arm64**.
2. A imagem vai para um registo (GHCR) com uma **tag imutável** (o SHA do commit; nunca `latest`).
3. A tag nova chega ao `gitops` por commit/PR (Argo CD Image Updater ou um passo do CI; manual no início).
4. O Argo CD de cada cluster vê a mudança e atualiza os pods. Rollback = reverter esse commit.

### 4. DAGs de vários projetos num só Airflow
- Os DAGs **deixam de estar dentro da imagem do Airflow** (hoje estão: cada mudança obriga a reconstruí-la).
- Cada projeto traz os seus DAGs por **git-sync**, na sua pasta.
- Cada tarefa corre com a **imagem do seu projeto** (KubernetesExecutor, `executor_config`/`pod_override`): as
  dependências de um projeto não colidem com as de outro.

### 5. Partilhado vs por projeto
| Plataforma | Por projeto |
|---|---|
| Airflow, Postgres (uma base por projeto), lakeFS (um repositório por projeto), OpenMetadata, Argo CD | código, imagens, DAGs, namespace, segredos, limites |

Na Oracle (2 CPU / 12 GB) cada projeto tem **namespace com ResourceQuota**, e a execução em série passa a valer para a
plataforma inteira: um **pool do Airflow** com poucas vagas, partilhado por todos os DAGs. Sem isto um projeto novo
pode deitar abaixo os outros.

### 6. Segredos
Hoje criados pelos scripts `install-*.sh` e fora do git. Num GitOps puro passam a ser declarados **cifrados**
(SOPS ou Sealed Secrets; a escolher num ADR próprio). Nunca em claro no git.

## Alternativas consideradas
- **Ficar com o monorepo:** simples, mas mistura código e ambientes e não serve outros projetos.
- **Repositório infra só do Modbus:** resolve a Oracle, mas obrigaria a duplicar a plataforma para cada projeto novo.
- **Um Airflow por projeto:** isolamento total, mas não cabe em 12 GB.
- **Flux em vez de Argo CD:** equivalente; o Argo CD já está instalado (ADR-005).

## Consequências
- Precisa de: registo de imagens, CI arm64, segredos cifrados, scripts de instalação convertidos em manifests do Argo CD.
- **Antes de separar:** o trabalho atual por commitar tem de ser revisto e commitado pelo Stéphane; a separação preserva a
  história (`git filter-repo`/`subtree`).
- **Antes de usar a Oracle:** criar o **orçamento/alerta de custos** na consola OCI e confirmar o teto do Always Free
  (2 OCPU / 12 GB / 200 GB, contando a `agente-01`). **Tarefa do Stéphane**, pré-requisito do `terraform apply`.
