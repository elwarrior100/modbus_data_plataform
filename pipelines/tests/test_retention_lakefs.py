"""A retenção da bronze contra o lakeFS REAL (precisa de LAKEFS_ENDPOINT e das chaves no ambiente).

Nunca toca no main: cria um branch de destino descartável, põe lá bronze sintético de 2020 (que não existe no main
real) e bronze de hoje, corre `expire_bronze(into=<descartável>)` e confirma que só o de 2020 saiu. No fim apaga só
os branches que o próprio teste criou."""
import os
import time
from datetime import date, datetime, timezone

import pytest

from silver.lake import Lake
from silver.retention import expire_bronze

pytestmark = [pytest.mark.lakefs, pytest.mark.skipif("LAKEFS_ENDPOINT" not in os.environ, reason="sem lakeFS")]


@pytest.fixture
def lake():
    lake = Lake.from_env()
    existing = {b.id for b in lake.repo.branches()}
    yield lake
    for branch in {b.id for b in lake.repo.branches()} - existing:
        lake.delete_branch(branch)


def test_old_bronze_leaves_the_target_new_bronze_and_silver_stay(lake):
    today = datetime.now(timezone.utc).date()
    target = f"retention-test-{int(time.time() * 1000)}"
    lake.ensure_branch(target, "main")
    old = [f"bronze/dt=2020-01-01/hour={h:02d}/t.parquet" for h in range(3)]
    new = f"bronze/dt={today}/hour=00/retention-test.parquet"
    for path in old + [new]:
        lake.upload(target, path, b"teste de retencao")
    assert lake.commit(target, "bronze sintético (teste de retenção)", {})
    silver_before = lake.list(target, "silver/")

    dry = expire_bronze(lake, today, dry_run=True, into=target)
    assert "2020-01-01" in dry.expired and lake.list(target, "bronze/dt=2020-01-01/") == old

    report = expire_bronze(lake, today, dry_run=False, into=target)
    assert report.merged and "2020-01-01" in report.expired
    assert lake.list(target, "bronze/dt=2020-01-01/") == []
    assert new in lake.list(target, f"bronze/dt={today}/")
    assert lake.list(target, "silver/") == silver_before
    assert expire_bronze(lake, today, dry_run=False, into=target).expired == []   # repetir não faz nada
