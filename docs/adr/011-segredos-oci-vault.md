# ADR-011: segredos num cofre (OCI Vault) lidos pelo External Secrets Operator

**Estado:** aceite e implementado (2026-11-14) · **Liga a:** ADR-010 (plataforma + projetos), docs/PLAN.md §12.1

## Contexto
Hoje há ~14 segredos (Postgres, SeaweedFS, lakeFS, ingest, Airflow, OpenMetadata, acesso do Argo CD ao git), todos
criados por scripts `platform/scripts/*.sh` diretamente em cada cluster. Nunca entraram no git, mas:
- não há uma fonte única: dois clusters (local e Oracle) teriam palavras-passe diferentes e sem registo;
- não há auditoria, nem rotação, nem forma de recuperar um segredo se o cluster se perder;
- o repositório `platform` vai ser **público** (ADR-010): nada sensível pode lá estar, nem cifrado por nós.

O Stéphane quer o equivalente ao Azure Key Vault: um cofre gerido, fora dos clusters.

## Decisão
1. **Cofre: OCI Vault** (tipo *default*, chave mestra protegida por software), na região de origem `eu-madrid-1`,
   num compartment próprio `platform-secrets`. É o equivalente direto ao Key Vault e serve os dois clusters.
2. **Ponte para o Kubernetes: External Secrets Operator (ESO)**, instalado pelo Argo CD. No git só há
   `ExternalSecret` ("o Secret X vem do segredo Y do cofre"); o ESO cria e atualiza o Secret do Kubernetes.
3. **Quem cria o quê:**
   - **Terraform** (repositório `platform`, `terraform/oracle/`): o compartment, o cofre, a chave mestra, um utilizador IAM só de
     leitura (`eso-reader`) e a política mínima (`read secret-bundles` só nesse compartment).
     **Nenhum valor de segredo passa pelo Terraform** (ficaria em claro no `tfstate`).
   - **Script `vault-seed.sh`**: cria cada segredo no cofre com a OCI CLI se ainda não existir. No cluster local,
     **copia os valores atuais** dos Secrets existentes (gerar novos partiria as bases de dados); valores novos
     são gerados aleatoriamente. Nada aparece no ecrã nem em argumentos.
4. **"Segredo zero"** (a credencial com que o ESO lê o cofre):
   - **Oracle:** *instance principal* — a própria VM é autorizada por política IAM; **não há chave nenhuma no cluster**.
   - **Local (Mac):** chave API do utilizador `eso-reader`, gerada localmente, guardada num único Secret criado por
     script. Só lê segredos, só deste compartment, e revoga-se na consola num minuto.
   - **Nunca** a chave de administrador de `~/.oci` (essa pode tudo no tenancy).
5. **Um segredo do cofre por Secret do Kubernetes**, com todas as chaves em JSON (`{"CHAVE": "valor", …}`), para o
   ESO o reconstruir com um só `dataFrom.extract`. Nome: `<namespace>-<secret>`, por exemplo `data-postgres-auth`.
   As cópias (`lakefs-credentials` em `ingest` e `airflow`) não se guardam: o ESO deriva-as de `data-lakefs-auth`.
   Os nomes aparecem no repositório público; os valores nunca.

**Feito em 2026-11-14:** cofre criado por Terraform (repositório `platform`, `terraform/oracle/vault.tf`) e 11 segredos
copiados do cluster local por `scripts/vault-seed.sh`; verificação por hash SHA-256: 11 iguais ao cluster, 0 diferentes.
Ficam fora do cofre, por agora, os Secrets gerados pelos próprios charts (`openmetadata-*`, `argocd-*`).

## Alternativas consideradas
- **Scripts por cluster (como hoje):** simples, mas sem fonte única, sem auditoria nem recuperação.
- **Sealed Secrets / SOPS:** segredos cifrados *dentro* do git. Funciona, mas num repositório público ficam expostos
  a ataques offline para sempre, e a chave de decifra fica nos clusters. Não é um cofre.
- **HashiCorp Vault / OpenBao no próprio cluster:** cofre a sério e portátil, mas vive dentro do cluster que serve
  (se o cluster cai, perde-se o cofre), e gasta RAM nos 12 GB da Oracle.
- **CSI driver do cofre (montar segredos como ficheiros):** não há um driver OCI maduro para k3s; o ESO cria Secrets
  normais, que todos os charts (Airflow, OpenMetadata) já sabem ler.

## Consequências
- Os scripts `install-*.sh` e `bootstrap-*.sh` deixam de criar Secrets: passam a pedir `ExternalSecret`.
- Pré-requisito de rede: os dois clusters precisam de saída HTTPS para a API do OCI Vault. Se o cofre não responder,
  os Secrets já criados continuam a funcionar (o ESO só falha a atualização).
- Rotação passa a ser possível (mudar no cofre → o ESO atualiza), mas as aplicações que leem a palavra-passe no
  arranque precisam de reiniciar. Fica para depois.
- Custos: o cofre *default* e as chaves de software constam do Always Free, com um limite de segredos por tenancy
  (uma fonte de terceiros diz 150; **confirmar na consola**, em Limits). Usamos ~14.

## A confirmar na implementação
- ~~Configuração do *provider* Oracle do ESO~~: confirmada na documentação do ESO (API `external-secrets.io/v1`,
  `principalType: InstancePrincipal` na Oracle; `UserPrincipal` com `privateKey` + `fingerprint` num Secret no local).
- Se o `eso-reader` precisa também de `read vaults` para listar, ou só de `read secret-bundles`.
