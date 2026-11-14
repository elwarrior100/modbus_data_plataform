# ADR-009: OpenMetadata no cluster (catálogo, linhagem, histórico dos gates)

**Estado:** aceite e implementado (2026-11-11; desde 2026-11-14 instalado pelo Argo CD) · **Fase:** 5 · **Liga a:** decisão D5 do plano (catálogo), ADR-008 (gates)

## Contexto
O plano exige o OpenMetadata como catálogo, linhagem e glossário, e como sítio onde fica o **histórico** dos gates
(G2 hoje, G3 depois). O que já existe: PostgreSQL 17 no namespace `data`, Airflow 3.2.2 no namespace `airflow`
(KubernetesExecutor), lakeFS com S3 gateway, bronze/silver em parquet. VM de 16 GB, com ~12 GB livres.

## Decisões

1. **Versão: OpenMetadata 2.0.7** (chart `openmetadata` e `openmetadata-dependencies`, ambos 2.0.7, a última linha
   publicada no repositório Helm a 2026-11-11). Alternativa: ficar na 1.13.x. Escolhi a 2.0 porque o Airflow 3 e o
   orquestrador nativo de Kubernetes já são o caminho suportado e não vale a pena começar numa linha que vamos migrar.
2. **Base de dados: o PostgreSQL que já temos**, numa base `openmetadata` com utilizador próprio (mesmo padrão do Airflow).
   Alternativa: o MySQL do chart. Rejeitada: seria mais um motor a gerir, e o plano já unifica tudo no Postgres.
3. **Pesquisa: OpenSearch do chart de dependências**, nó único, *heap* 1 GB, limite 2 GB (orçamento do plano §9).
4. **Execução das ingestões: orquestrador nativo de Kubernetes** (um `Job` por execução), **sem** o Airflow que vem no chart de
   dependências. Alternativa: usar o nosso Airflow como cliente do OpenMetadata. Rejeitada para já: exige volumes
   partilhados (RWX) que o k3s não tem, e duplica o que o Kubernetes já faz. Poupa também ~2–4 GB de RAM.
5. **Linhagem e histórico dos gates: empurrados por nós** para a API do OpenMetadata (cliente Python), a partir do
   código do pipeline (resultado de cada hora do G2 como resultado de teste; ligação bronze → silver). O catálogo
   não os adivinha sozinho: o lakeFS não é um conector nativo do OpenMetadata.
6. **Instalação por script** (`platform/scripts/install-openmetadata.sh`, idempotente, como o do Airflow), sem
   palavras-passe em argumentos nem no ecrã. Passar a Argo CD quando o git for atualizado (ADR-005 do plano).

## Consequências
- Mais ~3–4 GB de RAM (servidor ~1–2 GB + OpenSearch ~2 GB). O perfil `core+gov` do plano prevê ~16 GB.
- O serviço de pesquisa precisa de `vm.max_map_count` alto na VM (requisito do OpenSearch); fica registado no script.
- Os resultados do G2 passam a ter dois sítios: a atestação no lakeFS (impõe) e o OpenMetadata (mostra e guarda histórico).
  O hook continua a ser a única coisa que bloqueia.

## A confirmar durante a instalação
- Valores exatos do chart 2.0.7 para Postgres externo (o chart 1.12.4+ deixou de aceitar chaves separadas de palavra-passe).
- Se o conector de *datalake* lê parquet por S3 no SeaweedFS directamente ou pelo gateway S3 do lakeFS.
