from datetime import datetime, timezone

from ingest.envelope import Event, to_us

NOW = to_us(datetime(2026, 11, 7, 15, 30, 0, tzinfo=timezone.utc))
SEC = 1_000_000


def sample(n: int, *, node="ns=2;s=PUMP-01.outlet_pressure", value=None, status=0, source="ot-gateway",
           ingest_offset=0, source_ts="auto") -> Event:
    """A n-ésima amostra, com valores distintos para o event_id ser distinto."""
    raw = None if status >= 0x80000000 else (f"{5.0 + n / 1000:.3f}" if value is None else value)
    ts = NOW - 3600 * SEC + n * SEC
    return Event.build(source, node, raw, status, ts if source_ts == "auto" else source_ts, ts + 100,
                       NOW - 3600 * SEC + n * SEC + ingest_offset)
