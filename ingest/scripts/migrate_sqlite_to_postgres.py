"""Migração ÚNICA do spool SQLite antigo para o PostgreSQL (descartável depois de usada).
Corre com o volume antigo montado em /data. Recusa-se a correr se o Postgres já tiver dados.
Copia as amostras (com o lote a que pertencem) e os lotes: sem isto, o collector novo reescrevia
valores já exportados e o bronze ficava com duplicados."""
import os
import sqlite3
import sys

from ingest.spool import Spool

old = sqlite3.connect("file:/data/spool.db?mode=ro", uri=True)
spool = Spool(os.environ["DATABASE_URL"])

with spool._tx() as cur:
    cur.execute("SELECT (SELECT COUNT(*) FROM events), (SELECT COUNT(*) FROM batches)")
    if cur.fetchone() != (0, 0):
        sys.exit("o Postgres já tem dados: não migro por cima")

events = old.execute("SELECT event_id, source, opcua_node_id, raw_value, opcua_status_code, source_ts, server_ts, ingest_ts, batch_id "
                     "FROM events ORDER BY seq").fetchall()
batches = old.execute("SELECT batch_id, state, rows, detail FROM batches ORDER BY created_us").fetchall()
final = {"exported", "rejected"}
batches = [(b, s if s in final else "pending", r, d) for b, s, r, d in batches]    # estados intermédios antigos = pendente

with spool._tx() as cur:
    cur.executemany("INSERT INTO events (event_id, source, opcua_node_id, raw_value, opcua_status_code, source_ts, server_ts, "
                    "ingest_ts, batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", events)
    cur.executemany("INSERT INTO batches (batch_id, state, rows, detail) VALUES (%s,%s,%s,%s)", batches)

with spool._tx() as cur:
    cur.execute("SELECT COUNT(*) FROM events"); n_events = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM batches"); n_batches = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM events WHERE batch_id IS NULL"); unassigned = cur.fetchone()[0]
print(f"antigo: {len(events)} amostras, {len(batches)} lotes | novo: {n_events} amostras, {n_batches} lotes, {unassigned} por atribuir")
assert (n_events, n_batches) == (len(events), len(batches)), "as contagens não batem"
print("MIGRAÇÃO OK")
