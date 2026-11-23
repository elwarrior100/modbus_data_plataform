"""O gold no PostgreSQL para o BI (ADR-022, opção S1): por release, com vistas que seguem o `LATEST_STABLE`.

Em simples:
- cada **release** do lakeFS (`gold-v…`, ADR-019) é copiada para a base `serving`: as 9 tabelas do dia que ela cobre,
  cada linha com a coluna `release`. Nada se apaga quando sai uma release nova; um reprocessamento é outra release;
- `serving.latest_stable` é uma **cópia** do ponteiro `_releases/LATEST_STABLE` do lakeFS (uma linha);
- as vistas `gold.<tabela>` mostram, para cada dia, a release mais recente que **não passa o ponteiro**. É isto que o
  Superset e o Grafana leem. Um **rollback** muda a linha do ponteiro e as vistas mudam no mesmo instante, sem copiar;
- "mais recente" ordena-se pelo **carimbo** da tag (`_AAAAMMDDHHmm`, numérico) e depois pelo nome, nunca pelo texto da
  versão (em texto, v1.10 viria antes de v1.9). É a mesma ordem de `gold.release.releases`.

Fonte da verdade: o lakeFS. `sync` põe o Postgres igual a ele (carrega as releases que faltem e copia o ponteiro) e é
idempotente, por isso também corrige uma divergência (um rollback que mudou o lakeFS mas não chegou aqui, uma base
restaurada de um dump antigo). `check` só compara, para um alerta.

Tamanho: ~0,4 MB de gold por dia → ~150 MB por ano. As `dim_*` copiam-se inteiras em cada release (poucos KB).
"""
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq

from .build import TABLES
from .release import current, read_manifest, releases

log = logging.getLogger(__name__)

SERVING, VIEWS = "serving", "gold"


def _stamp(tag: str) -> int:
    return int(tag.rsplit("_", 1)[1])


def _pg_type(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_integer(t):
        return "bigint"
    if pa.types.is_floating(t):
        return "double precision"
    if pa.types.is_timestamp(t):
        return "timestamptz"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return "text[]"
    return "text"


@dataclass
class SyncReport:
    loaded: list[str] = field(default_factory=list)      # releases copiadas agora
    pointer: str | None = None                           # para onde o ponteiro ficou
    pointer_changed: bool = False
    rows: int = 0


class Serving:
    def __init__(self, dsn: str, *, serving: str = SERVING, views: str = VIEWS):
        self.dsn, self.s, self.v = dsn, serving, views
        with self._db() as db:
            db.execute(f"""
                CREATE SCHEMA IF NOT EXISTS {self.s};
                CREATE SCHEMA IF NOT EXISTS {self.v};
                CREATE TABLE IF NOT EXISTS {self.s}.releases (
                    tag TEXT PRIMARY KEY, stamp BIGINT NOT NULL, day DATE NOT NULL, g3 TEXT NOT NULL,
                    lakefs_commit TEXT NOT NULL, loaded_at TIMESTAMPTZ NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS {self.s}.latest_stable (
                    id BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
                    release TEXT NOT NULL REFERENCES {self.s}.releases (tag), set_at TIMESTAMPTZ NOT NULL);
                -- para cada dia, a release mais recente que não passa o ponteiro (por carimbo e nome, não pelo texto)
                CREATE OR REPLACE VIEW {self.s}.visible_releases AS
                SELECT DISTINCT ON (r.day) r.tag, r.day, r.g3
                  FROM {self.s}.releases r, {self.s}.latest_stable p JOIN {self.s}.releases rp ON rp.tag = p.release
                 WHERE (r.stamp, r.tag) <= (rp.stamp, rp.tag)
                 ORDER BY r.day, r.stamp DESC, r.tag DESC;
            """)

    def _db(self):
        db = psycopg.connect(self.dsn, autocommit=True)
        db.execute("SET timezone = 'UTC'")        # os instantes do gold são UTC sem fuso no parquet
        return db

    # ---- carregar uma release -----------------------------------------------------------------------------------
    def _ensure_table(self, cur, table: str, schema: pa.Schema) -> list[str]:
        cols = [f.name for f in schema]
        cur.execute(f"CREATE TABLE IF NOT EXISTS {self.s}.gold_{table} (release TEXT NOT NULL REFERENCES "
                    f"{self.s}.releases (tag))")
        for f in schema:                         # uma coluna nova (MINOR do esquema) acrescenta-se; nunca se apaga
            cur.execute(f'ALTER TABLE {self.s}.gold_{table} ADD COLUMN IF NOT EXISTS "{f.name}" {_pg_type(f.type)}')
        cur.execute(f"CREATE INDEX IF NOT EXISTS gold_{table}_release ON {self.s}.gold_{table} (release)")
        # a vista: as colunas todas da tabela (as de releases antigas sem uma coluna nova ficam NULL)
        cur.execute(f"CREATE OR REPLACE VIEW {self.v}.{table} AS SELECT g.* FROM {self.s}.gold_{table} g "
                    f"JOIN {self.s}.visible_releases v ON v.tag = g.release")
        return cols

    def load_release(self, lake, tag: str, commit: str) -> int:
        """Copia as tabelas do dia da release `tag` (lidas na própria tag). Idempotente: repetir substitui."""
        m = read_manifest(lake, tag)
        day = m["covers"]["day"]
        rows = 0
        with self._db() as db, db.transaction(), db.cursor() as cur:
            cur.execute(f"INSERT INTO {self.s}.releases (tag, stamp, day, g3, lakefs_commit) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (tag) DO NOTHING", (tag, _stamp(tag), day, m["gates"]["g3"], commit))
            for table in TABLES:
                path = next((p for p in m["tables"] if p.startswith(f"gold/{table}/")), None)
                if path is None:
                    continue                          # uma tabela que esta release não tem (esquema mais antigo)
                data = pq.read_table(io.BytesIO(lake.read(tag, path)))
                cols = self._ensure_table(cur, table, data.schema)
                cur.execute(f"DELETE FROM {self.s}.gold_{table} WHERE release = %s", (tag,))
                names = ", ".join(f'"{c}"' for c in cols)
                with cur.copy(f"COPY {self.s}.gold_{table} (release, {names}) FROM STDIN") as copy:
                    for rec in data.to_pylist():
                        copy.write_row([tag] + [rec[c] for c in cols])
                rows += data.num_rows
        return rows

    # ---- o ponteiro ---------------------------------------------------------------------------------------------
    def pointer(self) -> str | None:
        with self._db() as db:
            row = db.execute(f"SELECT release FROM {self.s}.latest_stable").fetchone()
            return row[0] if row else None

    def set_pointer(self, tag: str) -> float:
        """O ponteiro passa a `tag` (que já tem de estar carregada). Devolve os segundos: as vistas mudam logo."""
        started = time.monotonic()
        with self._db() as db:
            db.execute(f"INSERT INTO {self.s}.latest_stable (release, set_at) VALUES (%s, now()) "
                       "ON CONFLICT (id) DO UPDATE SET release = EXCLUDED.release, set_at = EXCLUDED.set_at", (tag,))
        return time.monotonic() - started

    def loaded(self) -> set[str]:
        with self._db() as db:
            return {r[0] for r in db.execute(f"SELECT tag FROM {self.s}.releases")}

    # ---- sincronizar com o lakeFS -------------------------------------------------------------------------------
    def sync(self, lake, ref: str = "main") -> SyncReport:
        """Carrega as releases do lakeFS que faltem e copia o ponteiro. Idempotente; corrige divergências."""
        report = SyncReport()
        have = self.loaded()
        for tag, commit in releases(lake):
            if tag not in have:
                report.rows += self.load_release(lake, tag, commit)
                report.loaded.append(tag)
        target = current(lake, ref)
        report.pointer = target
        if target is not None and target != self.pointer():
            self.set_pointer(target)
            report.pointer_changed = True
        return report

    def check(self, lake, ref: str = "main") -> list[str]:
        """O que diverge do lakeFS (vazio = igual): releases em falta e o ponteiro."""
        problems = []
        missing = sorted(t for t, _ in releases(lake) if t not in self.loaded())
        if missing:
            problems.append(f"releases por carregar: {', '.join(missing)}")
        lake_pointer, pg_pointer = current(lake, ref), self.pointer()
        if lake_pointer != pg_pointer:
            problems.append(f"ponteiro: lakeFS {lake_pointer} ≠ Postgres {pg_pointer}")
        return problems


def from_env() -> Serving | None:
    dsn = os.environ.get("SERVING_DATABASE_URL")
    return Serving(dsn) if dsn else None


def publish_pointer(tag: str) -> float | None:
    """Chamado por `gold.release.point` (release nova ou rollback), em melhor esforço: se falhar, o próximo `sync`
    (todos os dias, no gold_daily) corrige. Devolve os segundos no Postgres, ou None se não houve."""
    try:
        s = from_env()
        if s is None or tag not in s.loaded():
            return None                          # a release ainda não está aqui: o sync carrega-a e aponta
        seconds = s.set_pointer(tag)
        log.info("serving: LATEST_STABLE -> %s no Postgres em %.3f s (as vistas gold.* mudaram)", tag, seconds)
        return seconds
    except psycopg.Error as error:
        log.warning("serving: não consegui mover o ponteiro para %s (%s); o próximo sync corrige", tag, error)
        return None


if __name__ == "__main__":
    import sys

    from silver.lake import Lake

    s = from_env()
    if s is None:
        sys.exit("SERVING_DATABASE_URL não definido")
    lake = Lake.from_env()
    if sys.argv[1:] == ["check"]:
        problems = s.check(lake)
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False))
        sys.exit(1 if problems else 0)
    r = s.sync(lake)
    print(json.dumps(r.__dict__, ensure_ascii=False))
