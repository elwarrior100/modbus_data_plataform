"""DAG do gold: uma execução por dia UTC (ADR-019, D5: C1).

Corre às 02:40 UTC e trata o dia que FECHOU (logical_date − 1 dia). Porquê 02:40: o gold de D lê a silver até à
partição D+1 01h (as leituras de D que chegam depois da meia-noite), e essa fica no main às 01:25 + a duração; às 02:40
já lá está com folga. Não coincide com nada pesado: a silver às :25, a cópia às 03:00, a limpeza às 03:30.

`max_active_runs=1` e `catchup=True`: os dias correm um de cada vez e **por ordem**, o que importa aqui (o gold de D usa
o gold de D−1 para o estado inicial dos sinais que só se escrevem ao mudar, e para os turnos que atravessam a meia-noite).
A lógica está toda em `gold.run.run_day`, que é idempotente: repetir um dia é seguro.

Depois do gold: o catálogo no OpenMetadata (M1), a release (tag + manifest + `LATEST_STABLE`, ADR-019 D7/D9) e, numa
tarefa à parte (`serve`), a cópia das releases para o Postgres do BI (ADR-022, S1).
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

        from gold.release import release_day

        day = (logical_date - timedelta(days=1)).date()
        lake = Lake.from_env()
        try:
            result = run_day(lake, day)
        except GateFailed as error:                  # o G3 recusou o dia: repetir não muda nada, por isso não há retries
            raise AirflowFailException(str(error)) from error
        print(result)
        out = {"day": result.day, "skipped": result.skipped, "g3": result.g3_status, "samples": result.samples,
               "rows": result.rows, "silver_commit": result.silver_commit}
        if not result.skipped:
            from gold import openmetadata

            # o catálogo e o G3 do dia no OpenMetadata ANTES da release: assim as tabelas já existem quando o
            # LATEST_STABLE se mexe e a propriedade `release` de cada uma é atualizada (melhor esforço, nunca falha)
            openmetadata.publish(lake, day)
            # a release do dia (ADR-019 D7/D9): tag + manifest; o LATEST_STABLE só avança sozinho com G3 pass
            r = release_day(lake, day)
            print(r)
            out.update(release=r.tag, release_created=r.created, latest_stable_moved=r.pointer_moved)
        return out

    @task(executor_config=PROJECT_IMAGE)
    def serve(_gold: dict) -> dict:
        """O gold das releases no Postgres do BI (ADR-022, S1): carrega as releases que faltem e copia o LATEST_STABLE.
        Tarefa à parte, depois da release: se o Postgres falhar, a release já está feita e esta tarefa repete sozinha
        (retries); o `sync` é idempotente e corrige qualquer divergência com o lakeFS."""
        from gold import serving
        from silver.lake import Lake

        s = serving.from_env()
        if s is None:
            print("SERVING_DATABASE_URL não definido: o gold não vai para o Postgres neste ambiente")
            return {"skipped": True}
        lake = Lake.from_env()
        r = s.sync(lake)
        problems = s.check(lake)
        if problems:
            raise RuntimeError(f"o Postgres do BI ficou diferente do lakeFS: {problems}")
        print(r)
        return {"loaded": r.loaded, "pointer": r.pointer, "rows": r.rows}

    serve(silver_to_gold())
