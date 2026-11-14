"""Limpeza diária dos branches de trabalho do lakeFS (política em silver/retention.py e ADR-007).

Começa em simulação: com `APPLY = False` só regista o que apagaria. Passa a True depois de rever o relatório."""
from datetime import datetime, timezone

from airflow.sdk import DAG, task

# Each task runs in this project's own image (Airflow + the `silver` package), published by CI and chosen in the
# platform repository (MODBUS_PIPELINES_IMAGE). The DAG file itself comes from git (DAG bundle), not from an image.
import os

from kubernetes.client import models as k8s

_IMAGE = os.environ.get("MODBUS_PIPELINES_IMAGE")
PROJECT_IMAGE = {"pod_override": k8s.V1Pod(spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="base", image=_IMAGE)]))} if _IMAGE else {}

APPLY = False

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

    prune_branches()
