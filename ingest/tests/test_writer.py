import pyarrow as pa
import pytest

from fake_lake import FakeLake
from helpers import NOW, sample
from ingest import bronze
from ingest.bronze import load_nodes
from ingest.spool import Spool
from ingest.writer import Writer


class Crash(Exception):
    pass


def writer(spool, lake, hook=None, **kw):
    return Writer(spool, lake, load_nodes(), max_rows=10, max_age_s=0, now=lambda: NOW, hook=hook, **kw)


def lake_event_ids(lake):
    ids = []
    for data in lake.main_files().values():
        ids += bronze.from_parquet(data).column("event_id").to_pylist()
    return ids


def exported_ids(spool):
    rows = spool.db.execute("SELECT event_id FROM events WHERE batch_id IN (SELECT batch_id FROM batches WHERE state = 'exported')")
    return {r[0] for r in rows.fetchall()}


def fresh(dsn, n=25):
    spool = Spool(dsn)
    spool.add([sample(i) for i in range(n)])
    return spool, FakeLake()


def test_a_normal_run_exports_everything_to_main(dsn):
    spool, lake = fresh(dsn)
    done = writer(spool, lake).run_once()
    assert [state for _, state in done] == ["exported"] * 3                    # 10 + 10 + 5
    ids = lake_event_ids(lake)
    assert sorted(ids) == sorted(e.event_id for b in done for e in spool.rows(b[0]))
    assert len(ids) == len(set(ids)) == 25
    assert any(path.startswith("bronze/dt=2026-11-07/hour=14/") for path in lake.main_files())


def test_killing_the_writer_at_every_step_never_duplicates_or_loses(new_dsn):
    steps = []
    spool, lake = fresh(new_dsn())
    writer(spool, lake, hook=steps.append).run_once()
    assert len(steps) == 15                                                     # 5 pontos por lote x 3 lotes

    for crash_at in range(len(steps)):
        case = new_dsn()
        spool, lake = fresh(case)
        calls = {"n": 0}

        def die(step, crash_at=crash_at, calls=calls):
            calls["n"] += 1
            if calls["n"] - 1 == crash_at:
                raise Crash(step)

        with pytest.raises(Crash):
            writer(spool, lake, hook=die).run_once()
        spool.add([sample(i) for i in range(100, 103)])                        # chegam amostras enquanto está morto
        reopened = Spool(case)                                                # reinício: a base de dados é a mesma
        writer(reopened, lake).run_once()

        ids = lake_event_ids(lake)
        assert len(ids) == len(set(ids)) == 28, f"crash no passo {crash_at} ({steps[crash_at]})"
        assert reopened.counts()["batches"] == {"exported": reopened.counts()["batches"]["exported"]}
        assert set(ids) == exported_ids(reopened)


def test_a_malformed_sample_rejects_its_batch_and_never_reaches_main(dsn):
    spool, lake = fresh(dsn, n=9)
    spool.add([sample(500, node="ns=2;s=INVENTADO.x")])                        # 9 boas + 1 má = um lote de 10
    done = writer(spool, lake).run_once()
    assert [state for _, state in done] == ["rejected"]
    assert lake_event_ids(lake) == []                                          # o main não foi tocado
    branch = f"etl-{done[0][0]}"
    assert any(p.endswith("g1_report.json") for p in lake.branches[branch]["committed"])   # a autópsia está no branch
    assert spool.batch(done[0][0]).state == "rejected"
    assert "G1-03" in spool.db.execute("SELECT detail FROM batches").fetchone()[0]


def test_a_rejected_batch_does_not_block_the_good_ones_after_it(dsn):
    spool, lake = fresh(dsn, n=9)
    spool.add([sample(500, node="ns=2;s=INVENTADO.x")])
    spool.add([sample(i) for i in range(1000, 1010)])
    states = [s for _, s in writer(spool, lake).run_once()]
    assert states == ["rejected", "exported"]
    assert len(lake_event_ids(lake)) == 10


def test_uncommitted_files_never_reach_main(tmp_path):
    """A garantia do lakeFS de que o writer depende: o que está só no staging não se junta."""
    lake = FakeLake()
    lake.ensure_branch("b")
    lake.upload("b", "bronze/x.parquet", b"x")
    assert lake.merge("b", "main", "m") is False and lake.main_files("bronze/") == {}
