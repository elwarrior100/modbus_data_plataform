# Image for this project's Airflow TASKS: the official Airflow image plus the `silver` package (DuckDB, lakeFS,
# pyarrow). The DAG files are not baked in: Airflow reads them from this repository (DAG bundle, ADR-010), and each
# task pod runs this image (pod_override with MODBUS_PIPELINES_IMAGE, set in the platform repository).
FROM docker.io/apache/airflow:3.4.0

# Security: the official image still ships packages with CRITICAL vulnerabilities that already have fixes
# (found by the Trivy step in CI). Upgrade only those, and drop a developer tool (prek) that tasks never use.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade perl perl-base perl-modules-5.36 libperl5.36 libbrotli1 \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/python/bin/python3 -m pip install --no-cache-dir --upgrade pip   # the system pip vendors an old certifi
USER airflow
RUN pip install --no-cache-dir --upgrade pip "authlib>=1.9.0" "certifi>=2026.10.18" "uv>=0.13.4" \
    && rm -rf /home/airflow/.local/share/uv/tools/prek

COPY pyproject.toml /tmp/silver/pyproject.toml
COPY src /tmp/silver/src
RUN pip install --no-cache-dir /tmp/silver
