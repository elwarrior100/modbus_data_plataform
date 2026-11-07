"""DAG de teste: prova que o Airflow corre uma tarefa como um pod no cluster, na imagem certa."""
import socket
from datetime import datetime
from importlib.metadata import version

from airflow.sdk import DAG, task

with DAG("hello_k8s", start_date=datetime(2026, 11, 1), schedule=None, catchup=False, tags=["teste"]):

    @task
    def onde_estou() -> str:
        import duckdb  # noqa: F401  (só para provar que a imagem tem a biblioteca)
        import lakefs  # noqa: F401

        pod = socket.gethostname()
        print(f"a correr no pod {pod} · duckdb {version('duckdb')} · lakefs {version('lakefs')}")
        return pod

    onde_estou()
