"""DAG do gold: uma execução por dia UTC (ADR-019, D5: C1).

Corre às 02:40 UTC e trata o dia que FECHOU (logical_date − 1 dia). Porquê 02:40: o gold de D lê a silver até à
partição D+1 01h (as leituras de D que chegam depois da meia-noite), e essa fica no main às 01:25 + a duração; às 02:40
já lá está com folga. Não coincide com nada pesado: a silver às :25, a cópia às 03:00, a limpeza às 03:30.

`max_active_runs=1` e `catchup=True`: os dias correm um de cada vez e **por ordem**, o que importa aqui (o gold de D usa
o gold de D−1 para o estado inicial dos sinais que só se escrevem ao mudar, e para os turnos que atravessam a meia-noite).
A lógica está toda em `gold.run.run_day`, que é idempotente: repetir um dia é seguro.

TODO(release, ADR-019 D7/D9): a seguir ao merge, o manifest, a tag `gold-v…` e o `LATEST_STABLE` (próxima etapa).
TODO(OpenMetadata, ADR-019 D10/M1): as tabelas gold no catálogo, com a linhagem silver → gold_daily → gold.
"""
from datetime import datetime, timedelta, timezone

from airflow.sdk import DAG, task

# Each task runs in this project's own image (Airflow + the `silver` and `gold` packages), published by CI and chosen in
# the platform repository (MODBUS_PIPELINES_IMAGE). The DAG file itself comes from git (DAG bundle), not from an image.
import os

from kubernetes.client import models as k8s

_IMAGE = os.environ.get("MODBUS_PIPELINES_IMAGE")
PROJECT_IMAGE = {"pod_override": k8s.V1Pod(spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="base", image=_IMAGE)]))} if _IMAGE else {}

with DAG(
    "gold_daily",
    # o 1.º dia calculado é 23-11 (a correr a 24-11). Os dias anteriores fazem-se, se se quiser, com um backfill por
    # ordem (`airflow backfill create --dag-id gold_daily ...`); sem o dia anterior, o 1.º dia começa sem estado inicial.
    start_date=datetime(2026, 11, 24, 2, 40, tzinfo=timezone.utc),
    schedule="40 2 * * *",
    catchup=True,
    max_active_runs=1,                   # um dia de cada vez e por ordem (ver acima)
    default_args={"retries": 2, "retry_delay": timedelta(minutes=5)},
    tags=["gold"],
):

    @task(executor_config=PROJECT_IMAGE)
    def silver_to_gold(logical_date=None) -> dict:
        from airflow.sdk.exceptions import AirflowFailException

        from gold.run import run_day
        from silver.lake import Lake
        from silver.run import GateFailed

        day = (logical_date - timedelta(days=1)).date()
        try:
            result = run_day(Lake.from_env(), day)
        except GateFailed as error:                  # o G3 recusou o dia: repetir não muda nada, por isso não há retries
            raise AirflowFailException(str(error)) from error
        print(result)
        return {"day": result.day, "skipped": result.skipped, "g3": result.g3_status, "samples": result.samples,
                "rows": result.rows, "silver_commit": result.silver_commit}

    silver_to_gold()
