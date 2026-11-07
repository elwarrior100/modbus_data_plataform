"""O formato do bronze: esquema Parquet, partições e caminhos (docs/bronze-contract.md)."""
import io
from collections import defaultdict
from importlib import resources

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .envelope import SCHEMA_VERSION, Event, from_us

BRONZE_SCHEMA = pa.schema([
    ("event_id", pa.string()), ("source", pa.string()), ("opcua_node_id", pa.string()),
    ("raw_value", pa.string()), ("opcua_status_code", pa.uint32()),
    ("source_ts", pa.timestamp("us", tz="UTC")), ("server_ts", pa.timestamp("us", tz="UTC")),
    ("ingest_ts", pa.timestamp("us", tz="UTC")), ("schema_version", pa.string()), ("batch_id", pa.string()),
])


def load_nodes() -> dict[str, str]:
    return yaml.safe_load(resources.files("ingest").joinpath("nodes.yaml").read_text())["nodes"]


def to_table(events: list[Event], batch_id: str) -> pa.Table:
    return pa.table({
        "event_id": [e.event_id for e in events], "source": [e.source for e in events],
        "opcua_node_id": [e.opcua_node_id for e in events], "raw_value": [e.raw_value for e in events],
        "opcua_status_code": [e.opcua_status_code for e in events],
        "source_ts": [from_us(e.source_ts) for e in events], "server_ts": [from_us(e.server_ts) for e in events],
        "ingest_ts": [from_us(e.ingest_ts) for e in events],
        "schema_version": [SCHEMA_VERSION] * len(events), "batch_id": [batch_id] * len(events),
    }, schema=BRONZE_SCHEMA)


def partition_of(event: Event) -> tuple[str, str]:
    moment = from_us(event.ingest_ts)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H")


def path_for(prefix: str, dt: str, hour: str, batch_id: str) -> str:
    return f"{prefix}/dt={dt}/hour={hour}/{batch_id}.parquet"


def partitions(events: list[Event], batch_id: str, prefix: str = "bronze") -> dict[str, list[Event]]:
    """Um ficheiro por partição (dt, hour), pela hora em que o collector recebeu a amostra."""
    groups = defaultdict(list)
    for event in events:
        groups[partition_of(event)].append(event)
    return {path_for(prefix, dt, hour, batch_id): rows for (dt, hour), rows in sorted(groups.items())}


def to_parquet(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def from_parquet(data: bytes) -> pa.Table:
    return pq.read_table(io.BytesIO(data))
