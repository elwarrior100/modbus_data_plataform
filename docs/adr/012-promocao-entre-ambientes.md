# ADR-012: promoção entre ambientes (o Mac segue o `main`, a Oracle segue o `oracle`)

**Estado:** aceite e implementado (2026-11-14) · **Liga a:** ADR-010 (plataforma + projetos)

## Contexto
Os dois clusters liam o `main` do repositório `platform`: qualquer commit chegava à Oracle (o ambiente sempre ligado)
no mesmo instante em que chegava ao Mac. Um erro chegava a "produção" sem ter sido visto em lado nenhum.

## Decisão
- `envs/<env>/apps` é uma Kustomization: as aplicações partilhadas (`platform/argocd/apps`) mais as do ambiente.
- Na Oracle, um *patch* troca o branch de todas as aplicações que leem o git (etiquetas `platform/git-source`,
  `platform/values-source*`) e do ApplicationSet para `oracle`. Os charts Helm externos mantêm a sua versão.
- **Promover** = `scripts/promote-oracle.sh [commit]`: avança o branch `oracle` (só *fast-forward*), depois de a
  mudança ter corrido no Mac.
- A imagem das tarefas e o *DAG bundle* passam a ser fixados no **mesmo SHA**, mudados no mesmo commit.

## Alternativas
- **Tags por release** (`oracle-2026-11-14`): rastreio mais explícito, mas um passo a mais por mudança; o branch
  `oracle` já dá a história do que foi promovido.
- **Um repositório por ambiente:** duplicação e deriva.
- **Argo CD Image Updater:** automatiza a troca de tags, mas não resolve a promoção entre ambientes.

## Consequências
- Uma mudança só chega à Oracle quando alguém a promove. O Mac tem de estar ligado para a ver primeiro.
- Os segredos por ambiente (palavras-passe de admin do Argo CD e do OpenMetadata) já são diferentes; os das bases
  de dados ainda são partilhados pelo cofre (próximo passo: um prefixo por ambiente).
