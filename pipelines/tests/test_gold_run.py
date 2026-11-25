"""Uma execução do gold (um dia) contra um lakeFS em memória: o ciclo, a idempotência, as falhas a meio e o G3."""
import copy
import json
import tempfile
from datetime import date
from pathlib import Path

import duckdb

import pytest

from fixture_gold import SATURDAY, SUNDAY
from gold.build import TABLES, gold_path
from gold.run import run_day
from silver.run import GateFailed


@pytest.fixture(scope="module")
def with_saturday(gold_lake):
    """O lago da sessão (bronze e silver) com o gold de sábado já feito: calcula-se uma vez para o módulo."""
    lake = copy.deepcopy(gold_lake[0])
    run_day(lake, SATURDAY)
    return lake


@pytest.fixture
def lake(gold_lake):
    return copy.deepcopy(gold_lake[0])           # cada teste com o seu lago (o da sessão tem só bronze e silver)


@pytest.fixture
def lake_sat(with_saturday):
    return copy.deepcopy(with_saturday)


def gold(lake, day=SUNDAY):
    return {p: d for p, d in lake.branches["main"]["committed"].items() if p.startswith("gold/") and f"dt={day}" in p}


def gold_branches(lake):
    """Os branches de trabalho do gold (o da hora da silver recusada pelo G2, na fixture, não conta)."""
    return {b for b in lake.branches if b.startswith("gold-")}


def attestation(lake, day=SUNDAY):
    return json.loads(lake.read("main", f"_gates/g3/dt={day}.json"))


def test_two_days_in_order_end_up_in_main_with_their_proof(lake):
    saturday, sunday = run_day(lake, SATURDAY), run_day(lake, SUNDAY)
    assert saturday.g3_status == sunday.g3_status == "pass"
    assert set(gold(lake)) == {gold_path(t, SUNDAY) for t in TABLES} and len(gold(lake, SATURDAY)) == len(TABLES)
    att = attestation(lake)
    assert att["status"] == "pass" and set(att["files"]) == set(gold(lake))
    assert att["files"] == {p: lake.checksum("main", p) for p in gold(lake)}       # os checksums do que está no main
    assert gold_branches(lake) == set()                                            # o branch de trabalho foi apagado
    assert sunday.rows["kpi_asset_shift"] == 9 and sunday.samples > 40_000


def test_the_proof_says_where_the_gold_came_from_down_to_the_bronze(lake_sat):
    lake = lake_sat
    result = run_day(lake, SUNDAY)
    inputs = attestation(lake)["inputs"]
    assert inputs["silver_commit"] == result.silver_commit
    parts = inputs["silver_partitions"]
    assert len(parts) == 26 and min(parts) == "silver/dt=2026-10-31/hour=23/data.parquet"    # D−1 23h … D+1 01h
    assert all(p["g2"] in ("pass", "warn") for p in parts.values())
    bronze = {p["bronze_commit"] for p in parts.values()}
    assert all(b in lake.snapshots for b in bronze)                                # commits de bronze que existem (T1)
    some = sorted(parts)[3]
    hour = some.split("/")[1][3:], some.split("/")[2][5:]
    assert lake.list(parts[some]["bronze_commit"], f"bronze/dt={hour[0]}/hour={hour[1]}/")     # e têm o bronze dessa hora


def test_running_the_same_day_again_changes_nothing_even_after_main_moved(lake_sat):
    lake = lake_sat
    first = run_day(lake, SUNDAY)
    before, commits = dict(gold(lake)), lake.commits["main"]
    lake.branches["main"]["committed"]["outra/coisa.txt"] = b"o main avancou"   # outro commit, nada a ver com o gold
    again = run_day(lake, SUNDAY)
    assert gold(lake) == before and lake.commits["main"] == commits                 # mesmos bytes, nenhum commit novo
    assert again.silver_commit == first.silver_commit


@pytest.mark.parametrize("step", ["branch", "build", "upload", "g3", "commit", "verify"])
def test_a_failure_before_the_merge_never_touches_main_and_the_rerun_finishes(lake_sat, step):
    lake = lake_sat

    def die(name):
        if name == step:
            raise RuntimeError(f"morreu em {name}")

    main_before = dict(lake.branches["main"]["committed"])
    with pytest.raises(RuntimeError):
        run_day(lake, SUNDAY, hook=die)
    assert lake.branches["main"]["committed"] == main_before
    assert f"gold-{SUNDAY}" in lake.branches                                       # o trabalho por acabar ficou no branch
    result = run_day(lake, SUNDAY)
    assert result.g3_status == "pass" and len(gold(lake)) == len(TABLES) and gold_branches(lake) == set()


def test_a_day_that_fails_g3_stays_in_its_branch(lake_sat):
    lake = lake_sat
    for hour in range(5, 13):                                    # 8 horas de domingo sem silver no main (> 6: G3-04 bloqueia)
        del lake.branches["main"]["committed"][f"silver/dt={SUNDAY}/hour={hour:02d}/data.parquet"]
    with pytest.raises(GateFailed, match="G3-04"):
        run_day(lake, SUNDAY)
    assert gold(lake) == {} and f"gold-{SUNDAY}" in lake.branches
    att = json.loads(lake.read(f"gold-{SUNDAY}", f"_gates/g3/dt={SUNDAY}.json"))
    assert att["status"] == "fail"                                                # a prova do "não" fica para autópsia


def test_a_day_without_silver_is_skipped_and_creates_nothing(lake):
    result = run_day(lake, date(2026, 11, 5))
    assert result.skipped and gold_branches(lake) == set()
    assert not [p for p in lake.branches["main"]["committed"] if p.startswith(("gold/", "_gates/g3/"))]


def test_the_first_gold_day_takes_the_initial_state_from_the_older_silver(lake, lake_sat):
    """O 1.º dia do gold (na Oracle, 23-11) não tem D−1: o run_day dá ao build a silver anterior à janela, do mesmo
    commit, e o dia sai igual ao calculado por ordem (o estado das máquinas é o mesmo nas duas formas)."""
    first, in_order = run_day(lake, SUNDAY), run_day(lake_sat, SUNDAY)
    assert first.g3_status == in_order.g3_status == "pass"
    path = gold_path("fact_asset_hour", SUNDAY)
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        for name, lk in (("first", lake), ("in_order", lake_sat)):
            local = Path(tmp) / f"{name}.parquet"
            local.write_bytes(lk.read("main", path))
            rows.append(duckdb.sql(f"""SELECT asset_id, hour_utc, run_s, stopped_s, fault_s, maintenance_s, no_data_s
                                       FROM '{local}' ORDER BY 1, 2""").fetchall())
    assert rows[0] == rows[1]
    assert sum(r[-1] for r in rows[0]) == 600                     # só os 10 min sem comunicação do motor (dom. 20:00)
