"""DAG da silver: uma execução por hora de bronze.

Corre ao minuto 25 e trata a hora que FECHOU (logical_date - 1 h): ao minuto 25 de cada hora já não entram
dados novos na hora anterior e o último lote do writer já foi exportado. Processar a hora em curso daria
uma silver parcial (aprendido na primeira corrida real: 42 132 linhas na silver contra 42 887 no bronze).

`catchup=True` + `max_active_runs` baixo = o Airflow recupera sozinho as horas em atraso (backfill),
uma de cada vez. A lógica está toda em `silver.run.run_hour`, que é idempotente: repetir uma hora é seguro.
"""
from datetime import datetime, timedelta, timezone

from airflow.sdk import DAG, task

# Each task runs in this project's own image (Airflow + the `silver` package), published by CI and chosen in the
# platform repository (MODBUS_PIPELINES_IMAGE). The DAG file itself comes from git (DAG bundle), not from an image.
import os

from kubernetes.client import models as k8s

_IMAGE = os.environ.get("MODBUS_PIPELINES_IMAGE")
PROJECT_IMAGE = {"pod_override": k8s.V1Pod(spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="base", image=_IMAGE)]))} if _IMAGE else {}

with DAG(
    "silver_hourly",
    start_date=datetime(2026, 11, 7, 16, tzinfo=timezone.utc),  # primeira hora com bronze real
    # :25 (era :15, e antes :10): o writer junta lotes de 15 em 15 min (BATCH_AGE_S=900, ADR-020: 3x menos commits,
    # logo 3x menos metadados do lakeFS, que nunca se apagam). A última amostra de uma hora chega ao bronze até ~15 min
    # depois; ficam 10 min de margem, como antes.
    schedule="25 * * * *",
    catchup=True,
    max_active_runs=1,                   # em série: com 2 CPUs na Oracle uma hora de cada vez (docs/PLAN.md §9)
    default_args={"retries": 2, "retry_delay": timedelta(minutes=2)},
    tags=["silver"],
):

    @task(executor_config=PROJECT_IMAGE)
    def bronze_to_silver(logical_date=None) -> dict:
        from airflow.sdk.exceptions import AirflowFailException

        from silver.lake import Lake
        from silver.run import GateFailed, run_hour

        closed_hour = logical_date.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        try:
            result = run_hour(Lake.from_env(), closed_hour)
        except GateFailed as error:                  # o gate G2 recusou a hora: repetir não muda nada, por isso não há retries
            raise AirflowFailException(str(error)) from error
        print(result)
        return {"hour": result.hour, "skipped": result.skipped, "g2": result.g2_status,
                "bronze_rows": result.bronze_rows, "silver_rows": result.silver_rows}

    bronze_to_silver()
