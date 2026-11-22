"""O gate G2 sobre a semana sintética: cada incidente tem de dar o resultado que o contrato promete."""
import re
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

from fake_lake import FakeLake
from silver.catalog import load_catalog
from silver.g2 import evaluate
from silver.run import GateFailed, run_hour
from silver.transform import transform


@pytest.fixture(scope="module")
def reports(week):
    """{(dt, hora): Report} de todas as horas da semana."""
    by_hour = defaultdict(list)
    for path, data in week[1].items():
        m = re.search(r"dt=(\S+?)/hour=(\d+)/", path)
        by_hour[(m[1], int(m[2]))].append(data)
    catalog, out = load_catalog(), {}
    with tempfile.TemporaryDirectory() as tmp:
        for (dt, hh), blobs in sorted(by_hour.items()):
            files = []
            for i, blob in enumerate(blobs):
                files.append(Path(tmp) / f"b{i}.parquet")
                files[-1].write_bytes(blob)
            silver = Path(tmp) / "s.parquet"
            transform(files, silver, catalog)
            out[(dt, hh)] = evaluate(silver, datetime.fromisoformat(f"{dt}T{hh:02d}:00:00+00:00"), catalog)
    return out


def rules(report):
    return {(f.id, f.severity, f.asset) for f in report.findings}


def test_normal_hours_are_clean(reports):
    noisy = {k for k, r in reports.items() if r.status != "pass"}
    assert noisy == {("2026-11-01", 3), ("2026-11-02", 10), ("2026-11-03", 12), ("2026-11-03", 13),
                     ("2026-11-06", 9), ("2026-11-08", 0)}          # só os incidentes (e a hora final incompleta da fixture)


def test_comms_gap_fails_on_completeness(reports):
    r = reports[("2026-11-02", 10)]
    assert r.status == "fail" and ("G2-01", "blocker", "CHILLER-01") in rules(r)


def test_spike_is_only_a_warning(reports):
    r = reports[("2026-11-01", 3)]
    assert r.status == "warn" and {x[:2] for x in rules(r)} == {("G2-03", "warn")}


def test_clock_skew_is_only_a_warning(reports):
    for hour in (12, 13):
        r = reports[("2026-11-03", hour)]
        assert r.status == "warn" and ("G2-07", "warn", None) in rules(r)


def test_stuck_sensor_is_a_warning_on_that_tag(reports):
    r = reports[("2026-11-06", 9)]
    assert r.status == "warn" and ("G2-06", "warn", "MOTOR-01") in rules(r)
    assert "vibration_rms" in r.summary


def test_an_asset_stopped_all_hour_is_not_a_stuck_sensor(reports):
    """Parado, os sinais de processo do motor ficam a 0 e sem linhas: não é um sensor preso (ADR-019, D11)."""
    r = reports[("2026-11-07", 3)]                    # o motor está parado das 02:00 às 04:00
    assert r.status == "pass", r.summary


def test_a_silent_process_tag_while_the_asset_rotates_still_warns(tmp_path):
    """Se outro sinal de processo do ativo mexeu, a máquina rodou: um sinal calado continua a ser suspeito."""
    import duckdb
    catalog = load_catalog()
    silver = tmp_path / "s.parquet"
    duckdb.sql(f"""COPY (SELECT a AS asset_id, t AS tag_name, 'GOOD' AS quality, [] :: VARCHAR[] AS dq_flags,
                    TIMESTAMPTZ '2026-11-07 03:00:00+00' + INTERVAL (m) MINUTE AS ingest_ts,
                    TIMESTAMPTZ '2026-11-07 03:00:00+00' + INTERVAL (m) MINUTE AS ts_utc,
                    TIMESTAMPTZ '2026-11-07 03:00:00+00' + INTERVAL (m) MINUTE AS server_ts,
                    1.0 AS value_double, NULL :: BOOLEAN AS value_bool, NULL :: VARCHAR AS value_str,
                    a || t || m AS bronze_event_id
                  FROM (VALUES ('MOTOR-01', 'speed_rpm'), ('MOTOR-01', 'device_clock'), ('MOTOR-01', 'winding_temp'))
                       v(a, t), range(60) r(m)) TO '{silver}' (FORMAT parquet)""")
    report = evaluate(silver, datetime.fromisoformat("2026-11-07T03:00:00+00:00"),
                      [t for t in catalog if t.asset == "MOTOR-01"])
    silent = {f.detail.split()[0] for f in report.findings if f.id == "G2-06"}
    assert {"torque", "current", "vibration_rms", "power_kw"} <= silent


def test_the_attestation_binds_the_gate_result_to_the_file_checksum(reports):
    import json
    att = json.loads(reports[("2026-11-02", 10)].attestation({"silver/x.parquet": "abc"}))
    assert att["gate"] == "G2" and att["status"] == "fail" and att["files"] == {"silver/x.parquet": "abc"}
    assert any(r["id"] == "G2-01" and r["severity"] == "blocker" for r in att["rules"])


def test_a_blocked_hour_never_reaches_main_but_leaves_its_branch_and_the_proof(week):
    lake = FakeLake()
    lake.load_bronze(week[1])
    before = dict(lake.branches["main"]["committed"])
    with pytest.raises(GateFailed):
        run_hour(lake, datetime.fromisoformat("2026-11-02T10:00:00+00:00"))
    assert lake.branches["main"]["committed"] == before                          # o main não mudou
    branch = lake.branches["silver-2026-11-02-10"]["committed"]                  # o branch ficou para autópsia
    assert "silver/dt=2026-11-02/hour=10/data.parquet" in branch
    assert b'"status": "fail"' in branch["_gates/g2/dt=2026-11-02/hour=10.json"]   # com a prova do gate


def _silver_of(week, dt, hh, tmp):
    blobs = [data for path, data in week[1].items() if f"dt={dt}/hour={hh:02d}/" in path]
    files = []
    for i, blob in enumerate(blobs):
        files.append(Path(tmp) / f"b{i}.parquet")
        files[-1].write_bytes(blob)
    silver = Path(tmp) / "s.parquet"
    transform(files, silver, load_catalog())
    return silver


def _with_extra_row(silver, server_shift_us):
    """Acrescenta uma cópia de uma leitura analógica com outro valor, no MESMO source_ts (o relógio de 1 s do
    equipamento), com o server_ts deslocado de server_shift_us."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from datetime import timedelta
    rows = pq.read_table(silver).to_pylist()
    row = next(r for r in rows if r["value_double"] is not None and r["quality"] == "GOOD")
    extra = dict(row, value_double=row["value_double"] + 1.0, bronze_event_id=row["bronze_event_id"] + "-x",
                 server_ts=row["server_ts"] + timedelta(microseconds=server_shift_us))
    pq.write_table(pa.Table.from_pylist(rows + [extra], schema=pq.read_schema(silver)), silver)


def test_two_true_readings_in_the_same_second_are_not_a_conflict(week):
    """ADR-018: o source_ts tem a precisão do relógio do equipamento (1 s); o server_ts (ms) desempata."""
    with tempfile.TemporaryDirectory() as tmp:
        silver = _silver_of(week, "2026-11-01", 5, tmp)
        _with_extra_row(silver, 900_000)                                  # chegou 0,9 s depois
        report = evaluate(silver, datetime.fromisoformat("2026-11-01T05:00:00+00:00"), load_catalog())
        assert not any(f.id == "G2-02" for f in report.findings)


def test_same_instant_and_same_server_time_with_two_values_is_still_a_warning(week):
    with tempfile.TemporaryDirectory() as tmp:
        silver = _silver_of(week, "2026-11-01", 5, tmp)
        _with_extra_row(silver, 0)                                        # a MESMA leitura com dois valores
        report = evaluate(silver, datetime.fromisoformat("2026-11-01T05:00:00+00:00"), load_catalog())
        assert ("G2-02", "warn", None) in rules(report)
