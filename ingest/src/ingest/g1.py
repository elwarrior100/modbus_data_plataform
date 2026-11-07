"""Gate G1 (contrato): regras que um lote tem de cumprir antes de ir para o main."""
import json
from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa

from .bronze import BRONZE_SCHEMA
from .envelope import make_event_id, to_us

DAY_US = 86_400 * 1_000_000
BAD_BIT = 0x80000000            # StatusCode: os dois bits mais altos 10 = Bad, 01 = Uncertain
RANGES = {"uint16": (0, 0xFFFF), "uint32": (0, 0xFFFFFFFF)}


@dataclass
class Violation:
    rule: str
    detail: str


def is_bad(status: int) -> bool:
    return status >= BAD_BIT


def _type_ok(kind: str, value) -> bool:
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "double":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind in RANGES:
        return isinstance(value, int) and not isinstance(value, bool) and RANGES[kind][0] <= value <= RANGES[kind][1]
    if kind == "datetime":
        try:
            return isinstance(value, str) and datetime.fromisoformat(value) is not None
        except ValueError:
            return False
    return False


def validate(table: pa.Table, expected_rows: int, nodes: dict[str, str], now_us: int) -> list[Violation]:
    if table.schema != BRONZE_SCHEMA:
        return [Violation("G1-01", f"esquema diferente do contrato: {table.schema.names}")]
    found: list[Violation] = []
    if table.num_rows != expected_rows:
        found.append(Violation("G1-08", f"{table.num_rows} linhas no lakeFS, {expected_rows} no spool"))
    seen: set[str] = set()
    for row in table.to_pylist():
        who = f"{row['opcua_node_id']} @ {row['event_id']}"
        source_us, status = to_us(row["source_ts"]), row["opcua_status_code"]
        expected_id = make_event_id(row["source"], row["opcua_node_id"], source_us, status, row["raw_value"])
        if row["event_id"] in seen or row["event_id"] != expected_id:
            found.append(Violation("G1-02", f"event_id repetido ou não recalculável: {who}"))
        seen.add(row["event_id"])
        kind = nodes.get(row["opcua_node_id"])
        if kind is None:
            found.append(Violation("G1-03", f"nó desconhecido: {who}"))
        raw = row["raw_value"]
        if is_bad(status) != (raw is None) and not (status >> 30 == 1 and raw is not None):
            found.append(Violation("G1-05", f"estado {status:#x} incoerente com raw_value={raw!r}: {who}"))
        if raw is not None:
            try:
                value = json.loads(raw)
            except ValueError:
                found.append(Violation("G1-04", f"raw_value não é JSON: {who}"))
            else:
                if kind is not None and not _type_ok(kind, value):
                    found.append(Violation("G1-04", f"valor {raw} não é do tipo {kind}: {who}"))
        if source_us is None:
            if not is_bad(status):
                found.append(Violation("G1-06", f"source_ts ausente num valor não Bad: {who}"))
        elif not now_us - 365 * DAY_US <= source_us <= now_us + DAY_US:
            found.append(Violation("G1-06", f"source_ts implausível ({row['source_ts']}): {who}"))
        if to_us(row["ingest_ts"]) > now_us + 5 * 60 * 1_000_000:
            found.append(Violation("G1-07", f"ingest_ts no futuro: {who}"))
    return found


def report(violations: list[Violation]) -> dict:
    return {"passed": not violations, "violations": [v.__dict__ for v in violations[:200]],
            "total_violations": len(violations)}
