"""O critério principal da Fase 4: 7 dias em backfill dão exatamente o mesmo que 7 dias incrementais."""
import hashlib
import io
import random
from datetime import datetime, timedelta, timezone

import duckdb
import pyarrow.parquet as pq

from fake_lake import FakeLake
from fixture_bronze import START
from silver.run import GateFailed, run_hour

# as horas que o G2 tem de recusar: o comms_gap do chiller e a última "hora" da fixture (só tem 1 leitura)
BLOCKED = {"2026-11-02T10", "2026-11-08T00"}


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
    blocked = set()
    for hour in order:
        try:
            run_hour(lake, hour)
        except GateFailed:
            blocked.add(hour.strftime("%Y-%m-%dT%H"))
    return lake, blocked


def fingerprint(lake):
    return {p: hashlib.sha256(d).hexdigest() for p, d in lake.silver().items()}


def test_a_week_of_backfill_equals_a_week_of_incremental_runs(week):
    chronological = hours(week)
    shuffled = chronological[:]
    random.Random(7).shuffle(shuffled)                        # um backfill não tem de respeitar a ordem
    (incremental, blocked_a), (backfill, blocked_b) = process(week, chronological), process(week, shuffled)
    assert blocked_a == blocked_b == BLOCKED                  # o gate decide o mesmo, em qualquer ordem
    assert len(chronological) == 169 and len(incremental.silver()) == 169 - len(BLOCKED)
    assert fingerprint(incremental) == fingerprint(backfill)  # os 167 ficheiros são byte a byte iguais
    (again, _) = process(week, chronological + chronological[:24])        # repetir um dia inteiro por cima
    assert fingerprint(again) == fingerprint(incremental)


def test_the_silver_has_every_bronze_row_exactly_once(week):
    lake, _ = process(week, hours(week))
    events = [e for e in week[0]                                                # só as horas que o gate deixou entrar
              if datetime.fromtimestamp(e.ingest_ts / 1e6, timezone.utc).strftime("%Y-%m-%dT%H") not in BLOCKED]
    silver_rows = sum(pq.ParquetFile(io.BytesIO(d)).metadata.num_rows for d in lake.silver().values())
    assert silver_rows == len(events) and len(events) < len(week[0]) == 377_304   # 301 940 antes dos 4 sinais novos (ADR-019, D11)
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
    lake, _ = process(week, hours(week))
    paths = ",".join(f"'{p}'" for p in _dump(lake))
    q = lambda sql: duckdb.sql(sql.replace("SILVER", f"read_parquet([{paths}])")).fetchall()    # noqa: E731

    assert q("SELECT COUNT(*) FROM SILVER WHERE quality = 'BAD' AND (value_double IS NOT NULL OR value_bool IS NOT NULL OR value_str IS NOT NULL)")[0][0] == 0
    bad = q("SELECT asset_id, COUNT(*) FROM SILVER WHERE quality = 'BAD' GROUP BY 1")
    assert {a for a, _ in bad} == {"PUMP-01"}            # o reinício da bomba; a avaria do chiller foi recusada pelo G2 e não entra
    assert q("SELECT COUNT(*) FROM SILVER WHERE 'out_of_range' IN dq_flags AND asset_id = 'PUMP-01' AND tag_name = 'outlet_pressure'")[0][0] >= 1
    skew = q("SELECT DISTINCT asset_id FROM SILVER WHERE list_contains(dq_flags, 'clock_skew')")
    assert skew == [("MOTOR-01",)]                                                           # só o motor teve o relógio desalinhado
    fault = q("SELECT COUNT(*) FROM SILVER WHERE asset_id = 'PUMP-01' AND tag_name = 'state' AND value_str = 'FAULT'")[0][0]
    assert fault >= 1                                                                        # a bomba chegou a avariar (o drift)
    assert q("SELECT MIN(ts_utc), MAX(ts_utc) FROM SILVER")[0][0] >= START
