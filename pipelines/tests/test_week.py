"""O critério principal da Fase 4: 7 dias em backfill dão exatamente o mesmo que 7 dias incrementais."""
import hashlib
import io
import random
from datetime import datetime, timedelta, timezone

import duckdb
import pyarrow.parquet as pq

from fake_lake import FakeLake
from fixture_bronze import START
from silver.run import run_hour


def hours(week):
    """As horas que têm bronze, por ordem cronológica."""
    out = []
    for path in sorted(week[1]):
        dt, hh = path.split("/")[1][3:], path.split("/")[2][5:]
        out.append(datetime.fromisoformat(f"{dt}T{hh}:00:00+00:00"))
    return out


def process(week, order):
    lake = FakeLake()
    lake.load_bronze(week[1])
    for hour in order:
        run_hour(lake, hour)
    return lake


def fingerprint(lake):
    return {p: hashlib.sha256(d).hexdigest() for p, d in lake.silver().items()}


def test_a_week_of_backfill_equals_a_week_of_incremental_runs(week):
    chronological = hours(week)
    shuffled = chronological[:]
    random.Random(7).shuffle(shuffled)                        # um backfill não tem de respeitar a ordem
    incremental = process(week, chronological)
    backfill = process(week, shuffled)
    assert len(incremental.silver()) == len(chronological) == 169
    assert fingerprint(incremental) == fingerprint(backfill)  # os 169 ficheiros são byte a byte iguais
    again = process(week, chronological + chronological[:24])             # repetir um dia inteiro por cima
    assert fingerprint(again) == fingerprint(incremental)


def test_the_silver_has_every_bronze_row_exactly_once(week):
    lake = process(week, hours(week))
    events = week[0]
    silver_rows = sum(pq.ParquetFile(io.BytesIO(d)).metadata.num_rows for d in lake.silver().values())
    assert silver_rows == len(events) == 301_940
    ids = duckdb.sql("SELECT COUNT(*), COUNT(DISTINCT bronze_event_id) FROM read_parquet([" + ",".join(
        f"'{p}'" for p in _dump(lake)) + "])").fetchall()[0]
    assert ids == (len(events), len(events))


def _dump(lake):
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    paths = []
    for i, (p, d) in enumerate(sorted(lake.silver().items())):
        (tmp / f"{i:03d}.parquet").write_bytes(d)
        paths.append(str(tmp / f"{i:03d}.parquet"))
    return paths


def test_every_incident_leaves_its_mark_in_the_silver(week):
    lake = process(week, hours(week))
    paths = ",".join(f"'{p}'" for p in _dump(lake))
    q = lambda sql: duckdb.sql(sql.replace("SILVER", f"read_parquet([{paths}])")).fetchall()    # noqa: E731

    assert q("SELECT COUNT(*) FROM SILVER WHERE quality = 'BAD' AND (value_double IS NOT NULL OR value_bool IS NOT NULL OR value_str IS NOT NULL)")[0][0] == 0
    bad = q("SELECT asset_id, COUNT(*) FROM SILVER WHERE quality = 'BAD' GROUP BY 1")
    assert {a for a, _ in bad} == {"CHILLER-01", "PUMP-01"}                                   # a avaria do chiller e o reinício da bomba
    assert q("SELECT COUNT(*) FROM SILVER WHERE 'out_of_range' IN dq_flags AND asset_id = 'PUMP-01' AND tag_name = 'outlet_pressure'")[0][0] >= 1
    skew = q("SELECT DISTINCT asset_id FROM SILVER WHERE list_contains(dq_flags, 'clock_skew')")
    assert skew == [("MOTOR-01",)]                                                           # só o motor teve o relógio desalinhado
    fault = q("SELECT COUNT(*) FROM SILVER WHERE asset_id = 'PUMP-01' AND tag_name = 'state' AND value_str = 'FAULT'")[0][0]
    assert fault >= 1                                                                        # a bomba chegou a avariar (o drift)
    assert q("SELECT MIN(ts_utc), MAX(ts_utc) FROM SILVER")[0][0] >= START
