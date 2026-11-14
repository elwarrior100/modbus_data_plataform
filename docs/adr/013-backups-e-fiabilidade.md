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

8. **Monitorização em duas camadas:** Prometheus + Grafana dentro do cluster (`kube-prometheus-stack`, ~930 Mi na
   Oracle) e **alarmes da OCI fora da VM**, por email (VM sem métricas há 10 min, CPU, memória, e nenhum backup em
   26 h através da métrica `backup_success` que o job publica). O Grafana morre com a VM; os alarmes da OCI não.
9. **Airflow 3.3.2** (a 3.2.2 tinha vulnerabilidades críticas, a do próprio Airflow corrigida na 3.3.0); a imagem
   das tarefas atualiza os pacotes que o Trivy ainda marcava (0 críticas com correção). Testado no Mac, depois promovido.
10. **Admissão** (ValidatingAdmissionPolicy): sem pods `hostNetwork`, `privileged`, `hostPID`, `hostIPC` ou `hostPath`
    fora do `kube-system`, e cada ExternalSecret só lê segredos do cofre com o prefixo do seu namespace.

## Alternativas
- **Velero:** backup de volumes do Kubernetes; mais peças e um bucket S3 compatível (precisaria de chaves).
- **Réplica do Postgres:** não cabe nos 12 GB, e uma réplica copia também os erros.

11. **Cópia noturna dos objetos do lake** (`lake-sync`, rclone) para `platform-lake-backup`: `--immutable`, a VM não pode
    sobrescrever nem apagar lá; sinal `lake_sync_success` e alarme. O pg_dump escreve `_COMPLETE` no fim e o teste de
    restauro só usa execuções completas.
12. **Frescura da silver** (namespace `ops`): sinal `silver_fresh` a cada 30 min enquanto o último merge tem < 3 h;
    alarme quando falta. Alarmes de tamanho dos buckets (os 20 GB grátis).
13. **Isolamento do namespace `backup`** (o que tem mais credenciais): saída só para o Postgres, o SeaweedFS, os
    metadados e as APIs da OCI; Pod Security `baseline` em todos os namespaces.

## O que ainda falta
- Um ensaio completo de restauro do lake (base do lakeFS + objetos) num cluster de teste.
- O cluster local não tem backups (é o ambiente de desenvolvimento).
- Um só nó: se a VM se perder, a recuperação é a partir dos backups (RTO de horas, não de minutos).
