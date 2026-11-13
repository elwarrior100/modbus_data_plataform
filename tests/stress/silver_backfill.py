"""Teste de carga: transforma e avalia (G2) TODAS as horas de bronze do main, em paralelo, sem escrever no lakeFS.
Serve para medir CPU/memória de uma carga pesada com o orçamento da Oracle (2 CPU). Uso: dentro de um pod com
LAKEFS_* no ambiente: python silver_backfill.py [workers]"""
import sys, tempfile, time, resource
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from silver import g2
from silver.catalog import load_catalog
from silver.lake import Lake
from silver.transform import transform

lake, catalog = Lake.from_env(), load_catalog()
hours = sorted({p.rsplit("/", 1)[0] for p in lake.list("main", "bronze/") if p.endswith(".parquet") and len(p.split("/")) == 4})   # ignora sobras de testes antigos (dt=2026-03-*)
print(f"{len(hours)} horas de bronze", flush=True)

def one(prefix: str):
    dt, hh = prefix.split("/")[1][3:], prefix.split("/")[2][5:]
    hour = datetime.fromisoformat(f"{dt}T{hh}:00:00+00:00")
    with tempfile.TemporaryDirectory() as tmp:
        local = []
        for i, path in enumerate(p for p in lake.list("main", prefix + "/") if p.endswith(".parquet")):
            f = Path(tmp) / f"b{i}.parquet"; f.write_bytes(lake.read("main", path)); local.append(f)
        out = Path(tmp) / "s.parquet"
        r = transform(local, out, catalog)
        rep = g2.evaluate(out, hour, catalog)
        return r.bronze_rows, rep.status

t0, rows, status = time.time(), 0, {}
REPEAT = int(sys.argv[2]) if len(sys.argv) > 2 else 1       # repetir para manter a carga o tempo suficiente a medir
with ThreadPoolExecutor(int(sys.argv[1]) if len(sys.argv) > 1 else 2) as ex:
    for n, s in ex.map(one, hours * REPEAT):
        rows += n; status[s] = status.get(s, 0) + 1
u = resource.getrusage(resource.RUSAGE_SELF)
print(f"MEDIDO horas={len(hours) * REPEAT} linhas={rows} estados={status} parede={time.time()-t0:.0f}s cpu={u.ru_utime+u.ru_stime:.0f}s pico_RAM={u.ru_maxrss/1024:.0f}MB")
