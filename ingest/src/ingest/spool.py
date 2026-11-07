"""O spool: duas tabelas no PostgreSQL entre receber uma amostra e escrevê-la no lakeFS."""
from contextlib import contextmanager
from dataclasses import astuple, dataclass

import psycopg

from .envelope import Event, make_batch_id

TERMINAL = ("exported", "rejected")
COLUMNS = "event_id, source, opcua_node_id, raw_value, opcua_status_code, source_ts, server_ts, ingest_ts"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL, opcua_node_id TEXT NOT NULL, raw_value TEXT,
    opcua_status_code BIGINT NOT NULL,
    source_ts BIGINT, server_ts BIGINT, ingest_ts BIGINT NOT NULL,
    batch_id TEXT);
CREATE INDEX IF NOT EXISTS events_batch ON events (batch_id);
CREATE TABLE IF NOT EXISTS batches (
    seq BIGSERIAL PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, rows INTEGER NOT NULL, detail TEXT);
"""


@dataclass(frozen=True)
class Batch:
    batch_id: str
    state: str
    rows: int


class Spool:
    def __init__(self, dsn: str):
        self.dsn, self.db = dsn, None
        with self._tx() as cur:
            cur.execute(SCHEMA)

    @contextmanager
    def _tx(self):
        """Uma transação, numa ligação que se refaz sozinha se a anterior caiu (por exemplo, o Postgres reiniciou)."""
        try:
            if self.db is None or self.db.closed:
                self.db = psycopg.connect(self.dsn, autocommit=True)
            with self.db.transaction(), self.db.cursor() as cur:
                yield cur
        except psycopg.OperationalError:
            self.db = None
            raise

    def add(self, events: list[Event]) -> int:
        """Guarda as amostras novas e devolve quantas eram realmente novas (as repetidas são ignoradas)."""
        with self._tx() as cur:
            cur.executemany(f"INSERT INTO events ({COLUMNS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                            "ON CONFLICT (event_id) DO NOTHING", [astuple(e) for e in events])
            return cur.rowcount

    def assign_batch(self, max_rows: int, max_age_s: float, now_us: int) -> Batch | None:
        """Congela as amostras por atribuir num lote, se o lote encheu ou se a mais antiga já espera
        há tempo de mais. Depois de congelado, o lote nunca mais é reagrupado."""
        with self._tx() as cur:
            cur.execute("SELECT seq, event_id, ingest_ts FROM events WHERE batch_id IS NULL "
                        "ORDER BY seq LIMIT %s FOR UPDATE", (max_rows,))
            rows = cur.fetchall()
            if not rows or (len(rows) < max_rows and (now_us - rows[0][2]) < max_age_s * 1e6):
                return None
            batch_id = make_batch_id([r[1] for r in rows])
            cur.execute("UPDATE events SET batch_id = %s WHERE seq = ANY(%s)", (batch_id, [r[0] for r in rows]))
            cur.execute("INSERT INTO batches (batch_id, state, rows) VALUES (%s, 'pending', %s)", (batch_id, len(rows)))
            return Batch(batch_id, "pending", len(rows))

    def unfinished(self) -> list[Batch]:
        with self._tx() as cur:
            cur.execute("SELECT batch_id, state, rows FROM batches WHERE state <> ALL(%s) ORDER BY seq", (list(TERMINAL),))
            return [Batch(*r) for r in cur.fetchall()]

    def batch(self, batch_id: str) -> Batch | None:
        with self._tx() as cur:
            cur.execute("SELECT batch_id, state, rows FROM batches WHERE batch_id = %s", (batch_id,))
            row = cur.fetchone()
            return Batch(*row) if row else None

    def rows(self, batch_id: str) -> list[Event]:
        with self._tx() as cur:
            cur.execute(f"SELECT {COLUMNS} FROM events WHERE batch_id = %s ORDER BY seq", (batch_id,))
            return [Event(*r) for r in cur.fetchall()]

    def set_state(self, batch_id: str, state: str, detail: str | None = None) -> None:
        with self._tx() as cur:
            cur.execute("UPDATE batches SET state = %s, detail = %s WHERE batch_id = %s", (state, detail, batch_id))

    def counts(self) -> dict:
        with self._tx() as cur:
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE batch_id IS NULL) FROM events")
            events, unassigned = cur.fetchone()
            cur.execute("SELECT state, COUNT(*) FROM batches GROUP BY state")
            return {"events": events, "unassigned": unassigned, "batches": dict(cur.fetchall())}

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
