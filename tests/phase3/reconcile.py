"""Reconciliação: tudo o que o spool diz ter exportado está no lakeFS, uma só vez, e nada mais.
Corre no Mac (ambiente de ingest/), com o lakeFS acessível em LAKEFS_ENDPOINT."""
import os
import sys

from ingest import bronze
from ingest.lake import Lake

import psycopg

with psycopg.connect(os.environ["INGEST_DSN"]) as pg:       # o spool está no Postgres: vê-se de fora, sem entrar no pod
    exported = [r[0] for r in pg.execute("SELECT event_id FROM events WHERE batch_id IN (SELECT batch_id FROM batches WHERE state='exported')")]
    states = dict(pg.execute("SELECT state, COUNT(*) FROM batches GROUP BY state").fetchall())
    total = pg.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    unassigned = pg.execute("SELECT COUNT(*) FROM events WHERE batch_id IS NULL").fetchone()[0]
spool = {"exported": exported, "states": states, "total": total, "unassigned": unassigned}

lake = Lake(os.environ["LAKEFS_ENDPOINT"], os.environ["LAKEFS_ACCESS_KEY_ID"], os.environ["LAKEFS_SECRET_ACCESS_KEY"], "industrial-lake")
in_lake = []
# O bronze real tem sempre hour=HH no caminho. Os ficheiros bronze/dt=2026-03-0X/data_0.parquet do
# spike da Fase 0 (dados sintéticos, outro esquema) também estão no main e não fazem parte da ingestão.
files = [p for p in lake.list("main", "bronze/dt=") if "/hour=" in p]
for path in files:
    in_lake += bronze.from_parquet(lake.read("main", path)).column("event_id").to_pylist()

exported, lake_ids = set(spool["exported"]), set(in_lake)
missing, extra = exported - lake_ids, lake_ids - exported
duplicates = len(in_lake) - len(lake_ids)
print(f"spool : {spool['total']} amostras · exportadas {len(exported)} · por atribuir {spool['unassigned']} · lotes {spool['states']}")
print(f"lakeFS: {len(files)} ficheiros · {len(in_lake)} linhas · {len(lake_ids)} event_id distintos")
print(f"  em falta no lakeFS (exportadas mas ausentes): {len(missing)}")
print(f"  a mais no lakeFS (presentes sem estarem exportadas): {len(extra)}")
print(f"  duplicados no lakeFS: {duplicates}")
ok = not missing and not extra and not duplicates
print("RECONCILIAÇÃO OK" if ok else "RECONCILIAÇÃO FALHOU")
sys.stdout.flush()
# os._exit: o pyarrow pode ficar preso a destruir a sua thread pool na saída normal do Python,
# o que retinha o resultado e deixava o processo pendurado.
os._exit(0 if ok else 1)
