# ADR-013: backups e fiabilidade da Oracle

**Estado:** aceite e implementado (2026-11-14) · **Liga a:** ADR-008 (incidentes do lakeFS), ADR-011 (cofre)

## Contexto
A revisão crítica de 2026-11-14 deu 2/10 à fiabilidade: nenhum backup, e um reinício da VM matava os contentores a
meio de escrever (o que corrompeu o lakeFS duas vezes no Mac). O Postgres guarda o spool **e** os metadados do lakeFS:
perder o disco é perder a história do lake.

## Decisão
1. **Dump diário** (CronJob `pg-backup`, 02:30 UTC, só na Oracle): `pg_dumpall --globals-only` + `pg_dump -Fc` de cada
   base, enviados para o bucket `platform-backups` com a identidade da VM (*instance principal*). O Object Storage
   apaga os dumps com mais de 14 dias. A VM pode criar e ler, **não apagar** (testado).
2. **Teste de restauro** (`scripts/pg-restore-test.sh`): restaura o último backup em bases temporárias e compara as
   linhas tabela a tabela. Primeira corrida: RESTORE TEST OK (4 bases).
3. **Backup semanal do disco** de arranque (política de volume, 3 semanas: dentro dos 5 backups grátis).
4. **Encerramento gracioso** do kubelet (90 s, 30 s para pods críticos), provado com um reinício real: o Postgres
   voltou com "database system was shut down" (limpo, sem *recovery*) e o lakeFS ficou legível.
5. **Prioridade** (`PriorityClass platform-data`) para o Postgres e o SeaweedFS, e o OpenSearch limitado a 1 CPU.
6. **Imagens fixas por digest** (TimescaleDB e SeaweedFS): os dois clusters estavam a correr builds diferentes.
7. **Estado do Terraform** no bucket versionado `platform-tfstate`.

## Alternativas
- **Velero:** backup de volumes do Kubernetes; mais peças e um bucket S3 compatível (precisaria de chaves).
- **Réplica do Postgres:** não cabe nos 12 GB, e uma réplica copia também os erros.

## O que ainda falta
- O SeaweedFS (os objetos do lake) só está coberto pelo backup do disco, não por um backup próprio.
- O cluster local não tem backups (é o ambiente de desenvolvimento).
