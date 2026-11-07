"""As regras do contrato (docs/silver-contract.md), uma a uma, com amostras pequenas e à mão."""
import duckdb
import pyarrow.parquet as pq
import pytest

from ingest import bronze
from ingest.envelope import Event, to_us
from silver.catalog import load_catalog
from silver.transform import SilverError, transform

from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 11, 7, 15, 0, 0, tzinfo=timezone.utc)
CAT = load_catalog()
BAD, UNCERTAIN = 0x80310000, 0x40940000


def ev(node, raw, status=0, *, source=0, server=0, ingest=0, no_source=False):
    """Uma amostra; os tempos são segundos depois de T0."""
    at = lambda s: to_us(T0 + timedelta(seconds=s))    # noqa: E731
    return Event.build("ot-gateway", f"ns=2;s={node}", raw, status, None if no_source else at(source), at(server), at(ingest))


def run(tmp_path, events, name="b"):
    path = tmp_path / f"{name}.parquet"
    pq.write_table(bronze.to_table(events, "b1"), path)
    result = transform([path], tmp_path / f"{name}-silver.parquet", CAT)
    rows = duckdb.sql(f"SELECT * FROM '{tmp_path / (name + '-silver.parquet')}' ORDER BY ts_utc, tag_name").fetchall()
    cols = [c[0] for c in duckdb.sql(f"SELECT * FROM '{tmp_path / (name + '-silver.parquet')}'").description]
    return result, [dict(zip(cols, r)) for r in rows]


def test_a_normal_sample_is_typed_with_unit_and_no_warnings(tmp_path):
    _, (row,) = run(tmp_path, [ev("PUMP-01.outlet_pressure", "5.07")])
    assert (row["asset_id"], row["tag_name"], row["value_double"], row["unit"]) == ("PUMP-01", "outlet_pressure", 5.07, "BAR")
    assert (row["quality"], row["quality_reason"], row["dq_flags"]) == ("GOOD", "Good", [])
    assert row["value_bool"] is None and row["value_str"] is None


def test_every_kind_of_value_lands_in_the_right_column(tmp_path):
    _, rows = run(tmp_path, [ev("MOTOR-01.state", "2"), ev("PUMP-01.alarm", "true"), ev("PUMP-01.run_hours", "12345"),
                             ev("CHILLER-01.device_clock", '"2026-11-07T15:00:01+00:00"')])
    by_tag = {r["tag_name"]: r for r in rows}
    assert (by_tag["state"]["value_double"], by_tag["state"]["value_str"]) == (2.0, "RUNNING")
    assert by_tag["alarm"]["value_bool"] is True and by_tag["alarm"]["value_double"] is None
    assert by_tag["run_hours"]["value_double"] == 12345.0
    assert by_tag["device_clock"]["value_str"] == "2026-11-07T15:00:01+00:00" and by_tag["device_clock"]["value_double"] is None


def test_quality_comes_from_the_status_and_bad_never_has_a_value(tmp_path):
    result, rows = run(tmp_path, [ev("PUMP-01.flow_rate", "220.4"), ev("PUMP-01.outlet_pressure", "20.0", UNCERTAIN),
                                  ev("CHILLER-01.inlet_temp", None, BAD), ev("CHILLER-01.outlet_temp", None, 0x80990000)])
    q = {(r["tag_name"]): (r["quality"], r["quality_reason"]) for r in rows}
    assert q["flow_rate"] == ("GOOD", "Good")
    assert q["outlet_pressure"] == ("UNCERTAIN", "UncertainEngineeringUnitsExceeded")
    assert q["inlet_temp"] == ("BAD", "BadNoCommunication")
    assert q["outlet_temp"] == ("BAD", "0x80990000")                  # um código que não conhecemos aparece em hexadecimal
    assert result.quality == {"BAD": 2, "GOOD": 1, "UNCERTAIN": 1}
    for r in rows:
        if r["quality"] == "BAD":
            assert r["value_double"] is None and r["value_bool"] is None and r["value_str"] is None
            assert r["unit"] == "CEL"                                  # a unidade continua a vir do catálogo


def test_each_warning_fires_only_when_it_should(tmp_path):
    _, rows = run(tmp_path, [
        ev("PUMP-01.outlet_pressure", "20.0", source=0, server=0),                       # out_of_range
        ev("PUMP-01.flow_rate", "100", source=0, server=120),                            # clock_skew (120 s > 60 s)
        ev("PUMP-01.motor_current", "40", source=0, server=0, ingest=7200),              # late (2 h)
        ev("PUMP-01.bearing_temp", "55", no_source=True, server=0, ingest=10),           # ts_fallback
        ev("PUMP-01.vibration_rms", "2.5", source=0, server=30),                         # 30 s: dentro da tolerância
    ])
    flags = {r["tag_name"]: r["dq_flags"] for r in rows}
    assert flags["outlet_pressure"] == ["out_of_range"]
    assert flags["flow_rate"] == ["clock_skew"]
    assert flags["motor_current"] == ["late"]
    assert flags["bearing_temp"] == ["ts_fallback"]
    assert flags["vibration_rms"] == []
    ts = {r["tag_name"]: r["ts_utc"] for r in rows}
    assert ts["bearing_temp"] == T0 + timedelta(seconds=10)                              # sem source_ts, usa-se o ingest_ts


def test_bad_quality_is_also_a_warning_so_it_filters_without_comparing_text(tmp_path):
    _, (row,) = run(tmp_path, [ev("CHILLER-01.inlet_temp", None, BAD)])
    assert row["dq_flags"] == ["bad_quality"]


def test_one_bronze_row_gives_exactly_one_silver_row_and_the_output_is_deterministic(tmp_path):
    events = [ev(f"PUMP-01.flow_rate", f"{200 + i}", source=i, server=i, ingest=i) for i in range(50)]
    r1, _ = run(tmp_path, events, "a")
    r2, _ = run(tmp_path, list(reversed(events)), "b")                                   # chegam noutra ordem
    assert (r1.bronze_rows, r1.silver_rows) == (50, 50)
    assert (tmp_path / "a-silver.parquet").read_bytes() == (tmp_path / "b-silver.parquet").read_bytes()


def test_a_node_missing_from_the_catalog_fails_loudly(tmp_path):
    with pytest.raises(SilverError, match="INVENTADO"):
        run(tmp_path, [ev("PUMP-01.flow_rate", "1"), ev("INVENTADO.x", "1")])


def test_repeated_event_ids_in_the_bronze_fail(tmp_path):
    e = ev("PUMP-01.flow_rate", "1")
    with pytest.raises(SilverError, match="repetidos"):
        run(tmp_path, [e, e])


def test_a_good_sample_whose_value_cannot_be_read_fails(tmp_path):
    with pytest.raises(SilverError, match="ilegível"):
        run(tmp_path, [ev("PUMP-01.flow_rate", '"abc"')])
