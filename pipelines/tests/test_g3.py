"""Gate G3 (ADR-019, D6; docs/gates-contract.md, secção 6): cada regra apanha o que deve, num gold verdadeiro adulterado."""
import json
import random

import duckdb
import pytest

from fixture_gold import SATURDAY, SUNDAY
from gold import g3
from gold.build import gold_path
from test_gold_build import build, silver_files

ALL_HOURS = set(range(24))


@pytest.fixture(scope="module")
def sunday(gold_lake, tmp_path_factory):
    lake = gold_lake[0]
    tmp = tmp_path_factory.mktemp("g3")
    saturday = build(lake, tmp, SATURDAY)
    return build(lake, tmp, SUNDAY, previous=saturday), silver_files(lake, SUNDAY, tmp), tmp


def tampered(result, tmp, table, select):
    """Uma cópia dos ficheiros do gold em que `table` passa a ser `select` (`FROM T` = a tabela verdadeira)."""
    files = dict(result.files)
    original = files[gold_path(table, SUNDAY)]
    out = tmp / f"tampered-{table}-{abs(hash(select))}.parquet"
    source = f"read_parquet('{original}', hive_partitioning = false)"
    duckdb.sql(f"COPY ({select.replace('FROM T', 'FROM ' + source)}) TO '{out}' (FORMAT PARQUET)")
    files[gold_path(table, SUNDAY)] = out
    return files


def ids(report, severity=None):
    return sorted({f.id for f in report.findings if severity is None or f.severity == severity})


def test_the_real_gold_passes_and_without_history_volume_and_drift_are_only_info(sunday):
    result, silver, _ = sunday
    report = g3.evaluate(SUNDAY, result.files, silver, silver_hours=ALL_HOURS)
    assert report.status == "pass" and ids(report) == ["G3-05", "G3-06"] and ids(report, "info") == ["G3-05", "G3-06"]
    assert report.rows > 40_000 and report.summary == "sem achados"


def test_g3_01_a_missing_sample_is_a_blocker(sunday):
    result, silver, tmp = sunday
    files = tampered(result, tmp, "fact_tag_hour",
                     "SELECT * REPLACE (CASE WHEN tag_name = 'power_kw' AND hour(hour_utc) = 3 THEN samples - 1 ELSE samples END AS samples) FROM T")
    report = g3.evaluate(SUNDAY, files, silver, silver_hours=ALL_HOURS)
    assert report.status == "fail" and "G3-01" in ids(report, "blocker")


def test_g3_02_a_missing_hour_is_a_blocker(sunday):
    result, silver, tmp = sunday
    files = tampered(result, tmp, "fact_asset_hour", "SELECT * FROM T WHERE NOT (asset_id = 'MOTOR-01' AND hour(hour_utc) = 7)")
    assert ids(g3.evaluate(SUNDAY, files, silver, silver_hours=ALL_HOURS), "blocker") == ["G3-02"]


@pytest.mark.parametrize("table, select", [
    ("fact_asset_hour", "SELECT * REPLACE (run_s + 1 AS run_s) FROM T"),                    # 3601 segundos numa hora
    ("fact_asset_hour", "SELECT * REPLACE (-energy_kwh AS energy_kwh) FROM T"),
    ("kpi_asset_shift", "SELECT * REPLACE (CASE WHEN efficiency IS NOT NULL THEN 1.2 END AS efficiency) FROM T"),
    ("kpi_asset_day", "SELECT * REPLACE (CASE WHEN kwh_per_m3 IS NOT NULL THEN 3.0 END AS kwh_per_m3) FROM T"),
])
def test_g3_03_physics(sunday, table, select):
    result, silver, tmp = sunday
    assert ids(g3.evaluate(SUNDAY, tampered(result, tmp, table, select), silver, silver_hours=ALL_HOURS), "blocker") == ["G3-03"]


def test_g3_04_hours_refused_by_g2_warn_and_more_than_six_block(sunday):
    result, silver, _ = sunday
    few = g3.evaluate(SUNDAY, result.files, silver, silver_hours=ALL_HOURS - {10, 11})
    many = g3.evaluate(SUNDAY, result.files, silver, silver_hours=set(range(17)))
    assert few.status == "warn" and ids(few, "warn") == ["G3-04"] and "10h, 11h" in few.summary
    assert many.status == "fail" and ids(many, "blocker") == ["G3-04"]


def _history(result, tmp, days, change="T.mean_value", samples="T.samples"):
    """`days` dias anteriores falsos, feitos do próprio domingo (com o volume e as médias que se quiser)."""
    out = []
    for back in range(1, days + 1):
        path = tmp / f"hist-{back}-{abs(hash((change, samples)))}.parquet"
        duckdb.sql(f"""COPY (SELECT T.* REPLACE (T.hour_utc - INTERVAL {back} DAY AS hour_utc, {change} AS mean_value,
                       {samples} AS samples) FROM read_parquet('{result.files[gold_path('fact_tag_hour', SUNDAY)]}',
                       hive_partitioning = false) T) TO '{path}' (FORMAT PARQUET)""")
        out.append(path)
    return out


def test_g3_05_volume_against_the_previous_days(sunday):
    result, silver, tmp = sunday
    same = g3.evaluate(SUNDAY, result.files, silver, silver_hours=ALL_HOURS, history=_history(result, tmp, 3))
    assert "G3-05" not in ids(same)
    double = g3.evaluate(SUNDAY, result.files, silver, silver_hours=ALL_HOURS, history=_history(result, tmp, 3, samples="T.samples * 2"))
    assert ids(double, "warn") == ["G3-05"] and double.status == "warn"          # aviso, nunca bloqueio


def test_g3_06_drift_is_a_warning_once_there_is_history(sunday):
    result, silver, tmp = sunday
    stable = g3.evaluate(SUNDAY, result.files, silver, silver_hours=ALL_HOURS, history=_history(result, tmp, 3))
    assert "G3-06" not in ids(stable)
    moved = g3.evaluate(SUNDAY, result.files, silver, silver_hours=ALL_HOURS,
                        history=_history(result, tmp, 3, change="CASE WHEN T.tag_name = 'bearing_temp' THEN T.mean_value - 20 ELSE T.mean_value END"))
    drift = [f for f in moved.findings if f.id == "G3-06"]
    assert [(f.asset, f.severity) for f in drift] == [("PUMP-01", "warn")] and "bearing_temp" in drift[0].detail


def test_psi_is_small_for_the_same_distribution_and_large_for_a_shifted_one():
    rng = random.Random(1)
    ref = [rng.gauss(50, 1) for _ in range(168)]
    assert g3.psi(ref, [rng.gauss(50, 1) for _ in range(24)]) < 0.25
    assert g3.psi(ref, [rng.gauss(53, 1) for _ in range(24)]) > 0.25
    assert g3.psi(ref[:5], ref) is None                                           # pouco histórico: não se calcula


def test_the_attestation_carries_the_checksums_and_the_inputs(sunday):
    result, silver, _ = sunday
    report = g3.evaluate(SUNDAY, result.files, silver, silver_hours=ALL_HOURS)
    doc = json.loads(report.attestation({"gold/x/dt=2026-11-01/data.parquet": "abc"}, {"silver_commit": "c0ffee"}))
    assert doc["gate"] == "G3" and doc["status"] == "pass" and doc["partition"] == "2026-11-01"
    assert doc["files"] == {"gold/x/dt=2026-11-01/data.parquet": "abc"} and doc["inputs"]["silver_commit"] == "c0ffee"
    assert {r["id"] for r in doc["rules"]} == {"G3-05", "G3-06"}                    # os `info` ficam registados


def test_g3_07_an_asset_without_state_all_day_but_with_good_readings_is_a_warning(sunday):
    """O caso do MOTOR-01 na Oracle (23-11): leituras todas GOOD, mas o estado nunca conhecido, cobertura 0."""
    result, silver, tmp = sunday
    assert "G3-07" not in ids(g3.evaluate(SUNDAY, result.files, silver, silver_hours=ALL_HOURS))
    motor = "asset_id = 'MOTOR-01'"
    zero = ", ".join(f"CASE WHEN {motor} THEN 0 ELSE {c} END AS {c}" for c in
                     ("run_s", "starting_s", "stopped_s", "fault_s", "maintenance_s", "planned_with_data_s", "run_planned_s"))
    files = tampered(result, tmp, "fact_asset_hour",
                     f"SELECT * REPLACE ({zero}, CASE WHEN {motor} THEN 3600 ELSE no_data_s END AS no_data_s) FROM T")
    report = g3.evaluate(SUNDAY, files, silver, silver_hours=ALL_HOURS)
    found = [x for x in report.findings if x.id == "G3-07"]
    assert report.status == "warn" and [(x.severity, x.asset) for x in found] == [("warn", "MOTOR-01")]
    assert found[0].value > 10_000                                   # as leituras GOOD do motor nesse dia
