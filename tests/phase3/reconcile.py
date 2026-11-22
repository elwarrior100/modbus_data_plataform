"""Reconciliação: tudo o que o spool diz ter exportado está no lakeFS, uma só vez, e nada mais.
Corre no Mac (ambiente de ingest/), com o lakeFS acessível em LAKEFS_ENDPOINT.

Janela: o spool só guarda 7 dias de lotes exportados (spool-retention) e o bronze 15. "A mais no lakeFS" só conta a
partir do evento exportado mais antigo que o spool ainda tem; o que é mais velho já saiu do spool de propósito.
"Em falta" e "duplicados" verificam-se em tudo."""
import os
import sys

from ingest import bronze
from ingest.lake import Lake

import psycopg

with psycopg.connect(os.environ["INGEST_DSN"]) as pg:       # o spool está no Postgres: vê-se de fora, sem entrar no pod
    exported = [r[0] for r in pg.execute("SELECT event_id FROM events WHERE batch_id IN (SELECT batch_id FROM batches WHERE state='exported')")]
    # início da janela que o spool ainda guarda (µs desde 1970, como o ingest_ts do spool)
    window_us = pg.execute("SELECT MIN(e.ingest_ts) FROM events e JOIN batches b USING (batch_id) WHERE b.state='exported'").fetchone()[0]
    states = dict(pg.execute("SELECT state, COUNT(*) FROM batches GROUP BY state").fetchall())
    total = pg.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    unassigned = pg.execute("SELECT COUNT(*) FROM events WHERE batch_id IS NULL").fetchone()[0]
spool = {"exported": exported, "states": states, "total": total, "unassigned": unassigned}

lake = Lake(os.environ["LAKEFS_ENDPOINT"], os.environ["LAKEFS_ACCESS_KEY_ID"], os.environ["LAKEFS_SECRET_ACCESS_KEY"], "industrial-lake")
in_lake, in_window = [], []
# O bronze real tem sempre hour=HH no caminho. Os ficheiros bronze/dt=2026-03-0X/data_0.parquet do
# spike da Fase 0 (dados sintéticos, outro esquema) também estão no main e não fazem parte da ingestão.
files = [p for p in lake.list("main", "bronze/dt=") if "/hour=" in p]
for path in files:
    table = bronze.from_parquet(lake.read("main", path))
    ids = table.column("event_id").to_pylist()
    in_lake += ids
    if window_us is not None:
        ts = table.column("ingest_ts").cast("int64").to_pylist()   # timestamp(us) -> µs desde 1970
        in_window += [i for i, t in zip(ids, ts) if t >= window_us]

exported, lake_ids = set(spool["exported"]), set(in_lake)
missing, extra = exported - lake_ids, set(in_window) - exported
duplicates = len(in_lake) - len(lake_ids)
print(f"spool : {spool['total']} amostras · exportadas {len(exported)} · por atribuir {spool['unassigned']} · lotes {spool['states']}")
print(f"lakeFS: {len(files)} ficheiros · {len(in_lake)} linhas · {len(lake_ids)} event_id distintos · {len(set(in_window))} na janela do spool")
print(f"  em falta no lakeFS (exportadas mas ausentes): {len(missing)}")
print(f"  a mais no lakeFS, na janela do spool (presentes sem estarem exportadas): {len(extra)}")
print(f"  duplicados no lakeFS: {duplicates}")
ok = not missing and not extra and not duplicates
print("RECONCILIAÇÃO OK" if ok else "RECONCILIAÇÃO FALHOU")
sys.stdout.flush()
# os._exit: o pyarrow pode ficar preso a destruir a sua thread pool na saída normal do Python,
# o que retinha o resultado e deixava o processo pendurado.
os._exit(0 if ok else 1)
