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
