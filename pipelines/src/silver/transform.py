"""Bronze → silver com DuckDB: uma linha de bronze dá exatamente uma linha de silver (docs/silver-contract.md)."""
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from .catalog import Tag, to_arrow

VERSION = "2"                      # versão do código da transformação; entra em cada linha (silver_version)
                                   # 2 (ADR-018): dentro do mesmo segundo, as linhas ordenam-se pelo server_ts

# Os estados OPC UA que o gateway usa. Qualquer outro aparece como código em hexadecimal.
STATUS_NAMES = {
    0x00000000: "Good",
    0x80310000: "BadNoCommunication",
    0x80890000: "BadConfigurationError",
    0x808B0000: "BadDeviceFailure",
    0x80050000: "BadCommunicationError",
    0x80320000: "BadWaitingForInitialData",
    0x40940000: "UncertainEngineeringUnitsExceeded",
}
SKEW_US = 60 * 1_000_000           # clock_skew: relógio do dispositivo e do gateway diferem mais de 60 s
LATE_US = 3600 * 1_000_000         # late: recebida mais de 1 hora depois do instante do evento


class SilverError(Exception):
    """Algo no bronze não cumpre o contrato: a execução falha e o main fica intacto."""


@dataclass
class Result:
    bronze_rows: int
    silver_rows: int
    quality: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)


def _lit(text: str | Path) -> str:
    """Texto como literal SQL (plicas duplicadas). Só se usa com caminhos que o próprio código cria."""
    return "'" + str(text).replace("'", "''") + "'"


def _reason_sql() -> str:
    whens = " ".join(f"WHEN {code} THEN '{name}'" for code, name in STATUS_NAMES.items())
    return f"CASE opcua_status_code {whens} ELSE printf('0x%08X', opcua_status_code) END"


SQL = f"""
CREATE TEMP TABLE silver_rows AS
WITH joined AS (
    SELECT b.*, c.asset_id, c.tag_name, c.kind, c.unit, c.eu_min, c.eu_max, c.enum_labels,
           b.opcua_status_code >> 30 AS qclass
    FROM bronze b LEFT JOIN catalog c ON c.node_id = b.opcua_node_id
), typed AS (
    SELECT *,
        CASE WHEN kind IN ('analog', 'counter', 'state') THEN TRY_CAST(raw_value AS DOUBLE) END AS value_double,
        CASE WHEN kind = 'boolean' THEN TRY_CAST(raw_value AS BOOLEAN) END AS value_bool,
        CASE kind WHEN 'state' THEN enum_labels[TRY_CAST(raw_value AS INTEGER) + 1]
                  WHEN 'clock' THEN json_extract_string(raw_value, '$') END AS value_str,
        coalesce(source_ts, ingest_ts) AS ts_utc
    FROM joined
)
SELECT
    asset_id, tag_name, ts_utc, value_double, value_bool, value_str, unit,
    CASE qclass WHEN 0 THEN 'GOOD' WHEN 1 THEN 'UNCERTAIN' ELSE 'BAD' END AS quality,
    {_reason_sql()} AS quality_reason,
    list_filter([
        CASE WHEN kind = 'analog' AND value_double IS NOT NULL AND (value_double < eu_min OR value_double > eu_max) THEN 'out_of_range' END,
        CASE WHEN source_ts IS NOT NULL AND server_ts IS NOT NULL AND abs(epoch_us(server_ts) - epoch_us(source_ts)) > {SKEW_US} THEN 'clock_skew' END,
        CASE WHEN epoch_us(ingest_ts) - epoch_us(ts_utc) > {LATE_US} THEN 'late' END,
        CASE WHEN source_ts IS NULL THEN 'ts_fallback' END,
        CASE WHEN qclass >= 2 THEN 'bad_quality' END
    ], lambda x: x IS NOT NULL) AS dq_flags,
    ingest_ts, source_ts, server_ts, event_id AS bronze_event_id, batch_id AS bronze_batch_id, ? AS silver_version
FROM typed
ORDER BY ts_utc, asset_id, tag_name, server_ts, bronze_event_id
"""


def transform(bronze_files: list[str | Path], out_path: str | Path, catalog: list[Tag], version: str = VERSION) -> Result:
    """Lê os ficheiros do bronze, aplica as regras do contrato e escreve um Parquet. Se o bronze não
    cumprir o contrato (nó desconhecido, duplicados, valor ilegível), levanta SilverError."""
    con = duckdb.connect()
    con.register("catalog", to_arrow(catalog))
    files = "[" + ", ".join(_lit(f) for f in bronze_files) + "]"
    con.execute(f"CREATE VIEW bronze AS SELECT * FROM read_parquet({files})")

    bronze_rows, distinct_ids = con.execute("SELECT COUNT(*), COUNT(DISTINCT event_id) FROM bronze").fetchone()
    if distinct_ids != bronze_rows:
        raise SilverError(f"o bronze tem {bronze_rows - distinct_ids} event_id repetidos")
    unknown = [r[0] for r in con.execute("SELECT DISTINCT opcua_node_id FROM bronze WHERE opcua_node_id NOT IN (SELECT node_id FROM catalog)").fetchall()]
    if unknown:
        raise SilverError(f"nós que não estão no catálogo: {sorted(unknown)}")

    con.execute(SQL, [version])
    silver_rows = con.execute("SELECT COUNT(*) FROM silver_rows").fetchone()[0]
    if silver_rows != bronze_rows:
        raise SilverError(f"silver tem {silver_rows} linhas e o bronze {bronze_rows}")
    unreadable = con.execute("SELECT COUNT(*) FROM silver_rows WHERE quality <> 'BAD' AND value_double IS NULL "
                             "AND value_bool IS NULL AND value_str IS NULL").fetchone()[0]
    if unreadable:
        raise SilverError(f"{unreadable} amostras não Bad com valor ilegível")
    bad_with_value = con.execute("SELECT COUNT(*) FROM silver_rows WHERE quality = 'BAD' AND (value_double IS NOT NULL "
                                 "OR value_bool IS NOT NULL OR value_str IS NOT NULL)").fetchone()[0]
    if bad_with_value:
        raise SilverError(f"{bad_with_value} amostras Bad com valor")

    con.execute(f"COPY silver_rows TO {_lit(out_path)} (FORMAT PARQUET, COMPRESSION ZSTD)")
    quality = dict(con.execute("SELECT quality, COUNT(*) FROM silver_rows GROUP BY 1 ORDER BY 1").fetchall())
    flags = dict(con.execute("SELECT f, COUNT(*) FROM (SELECT unnest(dq_flags) AS f FROM silver_rows) GROUP BY 1 ORDER BY 1").fetchall())
    return Result(bronze_rows, silver_rows, quality, flags)
