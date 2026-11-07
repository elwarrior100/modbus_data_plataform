# Imagem do Airflow com o pacote `silver` (DuckDB, lakeFS, pyarrow) e os DAGs.
# O repositório é privado, por isso o código vai cozido na imagem em vez de git-sync (ADR-007).
FROM docker.io/apache/airflow:3.2.2

COPY pyproject.toml /tmp/silver/pyproject.toml
COPY src /tmp/silver/src
RUN pip install --no-cache-dir /tmp/silver

COPY dags /opt/airflow/dags
