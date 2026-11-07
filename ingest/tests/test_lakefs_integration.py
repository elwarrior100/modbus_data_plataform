"""O writer contra o lakeFS REAL do cluster (precisa de LAKEFS_ENDPOINT e das chaves no ambiente).
Escreve num branch de destino descartável, nunca no main."""
import os
import time

import pytest

from helpers import NOW, sample
from ingest import bronze
from ingest.bronze import load_nodes
from ingest.lake import Lake
from ingest.spool import Spool
from ingest.writer import Writer

pytestmark = pytest.mark.skipif("LAKEFS_ENDPOINT" not in os.environ, reason="sem lakeFS")


class Crash(Exception):
    pass


@pytest.fixture
def lake():
    lake = Lake(os.environ["LAKEFS_ENDPOINT"], os.environ["LAKEFS_ACCESS_KEY_ID"],
                os.environ["LAKEFS_SECRET_ACCESS_KEY"], os.environ.get("LAKEFS_REPOSITORY", "industrial-lake"))
    existing = {b.id for b in lake.repo.branches()}             # o que já lá estava nunca é tocado
    target = f"ingest-test-{int(time.time() * 1000)}"
    lake.ensure_branch(target, "main")
    yield lake, target
    for branch in {b.id for b in lake.repo.branches()} - existing:   # só apaga o que este teste criou
        lake.delete_branch(branch)


def ids_on(lake, target, prefix):
    out = []
    for path in lake.list(target, prefix):
        out += bronze.from_parquet(lake.read(target, path)).column("event_id").to_pylist()
    return out


def make_writer(spool, lake, target, hook=None):
    return Writer(spool, lake, load_nodes(), prefix="bronze-test", target=target, max_rows=10, max_age_s=0,
                  now=lambda: NOW, hook=hook)


@pytest.mark.lakefs
def test_real_lakefs_normal_run(dsn, lake):
    lake, target = lake
    spool = Spool(dsn)
    spool.add([sample(i) for i in range(25)])
    assert [s for _, s in make_writer(spool, lake, target).run_once()] == ["exported"] * 3
    ids = ids_on(lake, target, "bronze-test/dt=")
    assert len(ids) == len(set(ids)) == 25
    assert lake.list("main", "bronze-test/") == []                      # o main nunca foi tocado


@pytest.mark.lakefs
@pytest.mark.parametrize("crash_at", [0, 3, 5, 8, 11])
def test_real_lakefs_crash_and_restart_never_duplicates(dsn, lake, crash_at):
    lake, target = lake
    spool = Spool(dsn)
    spool.add([sample(i) for i in range(25)])
    calls = {"n": 0}

    def die(step):
        calls["n"] += 1
        if calls["n"] - 1 == crash_at:
            raise Crash(step)

    with pytest.raises(Crash):
        make_writer(spool, lake, target, hook=die).run_once()
    spool.add([sample(i) for i in range(100, 103)])
    make_writer(Spool(dsn), lake, target).run_once()
    ids = ids_on(lake, target, "bronze-test/dt=")
    assert len(ids) == len(set(ids)) == 28


@pytest.mark.lakefs
def test_real_lakefs_g1_rejection_leaves_target_untouched(dsn, lake):
    lake, target = lake
    spool = Spool(dsn)
    spool.add([sample(i) for i in range(9)] + [sample(500, node="ns=2;s=INVENTADO.x")])
    done = make_writer(spool, lake, target).run_once()
    assert [s for _, s in done] == ["rejected"]
    assert lake.list(target, "bronze-test/dt=") == []                   # nada chegou ao destino
    branch = f"etl-{done[0][0]}"
    assert any(p.endswith("g1_report.json") for p in lake.list(branch, "bronze-test/_rejected/"))
