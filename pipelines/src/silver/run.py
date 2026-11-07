"""Uma execução do pipeline: uma hora de bronze → silver, num branch, e só então para o main."""
import io
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from .catalog import load_catalog
from .transform import VERSION, SilverError, transform


@dataclass
class RunResult:
    hour: str
    skipped: bool = False
    bronze_rows: int = 0
    silver_rows: int = 0


def partition(hour: datetime) -> tuple[str, str]:
    hour = hour.astimezone(timezone.utc)
    return hour.strftime("%Y-%m-%d"), hour.strftime("%H")


def run_hour(lake, hour: datetime, *, target: str = "main", hook=None, keep_branch: bool = False) -> RunResult:
    """Processa uma hora. Cada passo é idempotente: repetir a hora dá o mesmo resultado, e se algo falhar
    a meio o `target` fica intacto (o que ficou por acabar fica no branch)."""
    hook = hook or (lambda step: None)          # os testes usam-no para simular uma falha em qualquer passo
    dt, hh = partition(hour)
    bronze_files = [p for p in lake.list(target, f"bronze/dt={dt}/hour={hh}/") if p.endswith(".parquet")]
    if not bronze_files:
        return RunResult(f"{dt}T{hh}", skipped=True)

    branch, silver_path = f"silver-{dt}-{hh}", f"silver/dt={dt}/hour={hh}/data.parquet"
    lake.ensure_branch(branch, target)
    hook("branch")
    with tempfile.TemporaryDirectory() as tmp:
        local = []
        for i, path in enumerate(bronze_files):
            local.append(Path(tmp) / f"bronze{i}.parquet")
            local[-1].write_bytes(lake.read(branch, path))
        out = Path(tmp) / "silver.parquet"
        result = transform(local, out, load_catalog())          # levanta SilverError se o bronze não cumprir o contrato
        hook("transform")
        lake.upload(branch, silver_path, out.read_bytes())
    hook("upload")

    lake.commit(branch, f"silver: {dt} hora {hh}", {"bronze_files": str(len(bronze_files)), "bronze_rows": str(result.bronze_rows),
                                                  "silver_rows": str(result.silver_rows), "silver_version": VERSION})
    hook("commit")
    stored = pq.ParquetFile(io.BytesIO(lake.read(branch, silver_path))).metadata.num_rows        # o que está no lakeFS é o que calculámos?
    if stored != result.bronze_rows:
        raise SilverError(f"no lakeFS há {stored} linhas de silver e o bronze tem {result.bronze_rows}")
    hook("verify")

    lake.merge(branch, target, f"silver: {dt} hora {hh}")
    hook("merge")
    if not keep_branch:
        lake.delete_branch(branch)
    return RunResult(f"{dt}T{hh}", False, result.bronze_rows, result.silver_rows)
