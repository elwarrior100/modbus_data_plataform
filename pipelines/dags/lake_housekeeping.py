"""Limpeza diária do lake (políticas em silver/retention.py; ADR-007 e ADR-015).

- prune_branches: branches de trabalho velhos (juntados há > 7 dias, por juntar há > 30). Aplicada desde 2026-11-15
  (o relatório em simulação listou só branches `etl-*` já juntados); sem isto a GC do lakeFS não liberta a bronze.
- expire_bronze: bronze crua com mais de 15 dias sai do main (decisão de 2026-11-14). Aplicada (`BRONZE_APPLY`).
  A silver e as atestações dos gates ficam para sempre."""
from datetime import datetime, timezone

from airflow.sdk import DAG, task

# Each task runs in this project's own image (Airflow + the `silver` package), published by CI and chosen in the
# platform repository (MODBUS_PIPELINES_IMAGE). The DAG file itself comes from git (DAG bundle), not from an image.
import os

from kubernetes.client import models as k8s

_IMAGE = os.environ.get("MODBUS_PIPELINES_IMAGE")
PROJECT_IMAGE = {"pod_override": k8s.V1Pod(spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="base", image=_IMAGE)]))} if _IMAGE else {}

APPLY = True
BRONZE_APPLY = True
SILVER_APPLY = False      # retenção da silver (ADR-020): em simulação até rever o 1.º relatório (fevereiro de 2027)

with DAG("lake_housekeeping", start_date=datetime(2026, 11, 9, tzinfo=timezone.utc), schedule="30 3 * * *",
         catchup=False, tags=["manutenção"]):

    @task(executor_config=PROJECT_IMAGE)
    def prune_branches() -> dict:
        import time

        from silver.lake import Lake
        from silver.retention import prune

        report = prune(Lake.from_env(), time.time(), dry_run=not APPLY)
        print(f"{'apagados' if APPLY else 'apagaria'}: {len(report.delete)} {report.delete[:10]}")
        return {"deleted": len(report.delete), "applied": APPLY, "keep_merged": report.keep_merged,
                "keep_unmerged": report.keep_unmerged, "ignored": report.ignored}

    @task(executor_config=PROJECT_IMAGE)
    def expire_bronze() -> dict:
        from datetime import datetime, timezone

        from silver.lake import Lake
        from silver.retention import KEEP_BRONZE_DAYS, expire_bronze

        today = datetime.now(timezone.utc).date()
        report = expire_bronze(Lake.from_env(), today, dry_run=not BRONZE_APPLY)
        print(f"bronze ({KEEP_BRONZE_DAYS} dias): {'saíram' if BRONZE_APPLY else 'sairiam'} {len(report.expired)} dias, "
              f"{report.objects} objetos {report.expired[:5]}; ficam {report.kept} dias; merge: {report.merged}")
        return {"expired": report.expired, "objects": report.objects, "kept": report.kept,
                "merged": report.merged, "applied": BRONZE_APPLY}

    @task(executor_config=PROJECT_IMAGE)
    def expire_silver() -> dict:
        """Silver com mais de 3 meses, só dos dias com release do gold (ADR-020). Primeiro dia elegível: ~2027-02-23.
        Começa em simulação (SILVER_APPLY = False): liga-se depois de rever o primeiro relatório com dias a sair."""
        from datetime import datetime, timezone

        from gold.retention import KEEP_SILVER_DAYS, expire_silver
        from silver.lake import Lake

        today = datetime.now(timezone.utc).date()
        report = expire_silver(Lake.from_env(), today, dry_run=not SILVER_APPLY)
        print(f"silver ({KEEP_SILVER_DAYS} dias): {'saíram' if SILVER_APPLY else 'sairiam'} {len(report.expired)} dias, "
              f"{report.objects} objetos {report.expired[:5]}; à espera de release: {report.waiting[:5]}; "
              f"ficam {report.kept} dias; merge: {report.merged}")
        return {"expired": report.expired, "objects": report.objects, "waiting": report.waiting, "kept": report.kept,
                "merged": report.merged, "applied": SILVER_APPLY}

    prune_branches()
    expire_bronze()
    expire_silver()
