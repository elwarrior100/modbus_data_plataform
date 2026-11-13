"""DAG da silver: uma execução por hora de bronze.

Corre ao minuto 10 e trata a hora que FECHOU (logical_date - 1 h): ao minuto 10 de cada hora já não entram
dados novos na hora anterior e o último lote do writer já foi exportado. Processar a hora em curso daria
uma silver parcial (aprendido na primeira corrida real: 42 132 linhas na silver contra 42 887 no bronze).

`catchup=True` + `max_active_runs` baixo = o Airflow recupera sozinho as horas em atraso (backfill),
uma de cada vez. A lógica está toda em `silver.run.run_hour`, que é idempotente: repetir uma hora é seguro.
"""
from datetime import datetime, timedelta, timezone

from airflow.sdk import DAG, task

with DAG(
    "silver_hourly",
    start_date=datetime(2026, 11, 7, 16, tzinfo=timezone.utc),  # primeira hora com bronze real
    schedule="10 * * * *",
    catchup=True,
    max_active_runs=1,                   # em série: com 2 CPUs na Oracle uma hora de cada vez (docs/PLAN.md §9)
    default_args={"retries": 2, "retry_delay": timedelta(minutes=2)},
    tags=["silver"],
):

    @task
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
