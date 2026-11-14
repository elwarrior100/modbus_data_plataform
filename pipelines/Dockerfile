# Image for this project's Airflow TASKS: the official Airflow image plus the `silver` package (DuckDB, lakeFS,
# pyarrow). The DAG files are not baked in: Airflow reads them from this repository (DAG bundle, ADR-010), and each
# task pod runs this image (pod_override with MODBUS_PIPELINES_IMAGE, set in the platform repository).
FROM docker.io/apache/airflow:3.2.2

COPY pyproject.toml /tmp/silver/pyproject.toml
COPY src /tmp/silver/src
RUN pip install --no-cache-dir /tmp/silver

