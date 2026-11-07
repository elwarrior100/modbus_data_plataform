"""O envelope do bronze: uma amostra tal como chegou, sem interpretação (docs/bronze-contract.md)."""
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "1"
NAMESPACE = uuid.UUID("6f4c1c2e-0b53-4d3a-9d2f-0c3b8a7e5a11")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_us(moment: datetime | None) -> int | None:
    """Datetime com fuso -> microssegundos desde 1970 (inteiro exato, sem erros de vírgula flutuante)."""
    if moment is None:
        return None
    delta = moment.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_us(value: int | None) -> datetime | None:
    return None if value is None else EPOCH + timedelta(microseconds=value)


def make_event_id(source: str, node_id: str, source_us: int | None, status: int, raw_value: str | None) -> str:
    """UUID v5: a mesma amostra dá sempre o mesmo id, e é recalculável (regra G1-02)."""
    key = "|".join([source, node_id, "" if source_us is None else str(source_us), str(status),
                    "" if raw_value is None else raw_value])
    return str(uuid.uuid5(NAMESPACE, key))


def make_batch_id(event_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(event_ids)).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Event:
    event_id: str
    source: str
    opcua_node_id: str
    raw_value: str | None
    opcua_status_code: int
    source_ts: int | None       # microssegundos UTC
    server_ts: int | None
    ingest_ts: int

    @classmethod
    def build(cls, source, node_id, raw_value, status, source_ts, server_ts, ingest_ts) -> "Event":
        return cls(make_event_id(source, node_id, source_ts, status, raw_value), source, node_id,
                   raw_value, status, source_ts, server_ts, ingest_ts)
