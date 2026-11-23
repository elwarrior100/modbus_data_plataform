"""Uma execução do pipeline: uma hora de bronze → silver, num branch, e só então para o main."""
import io
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from . import g2, openmetadata
from .catalog import load_catalog
from .transform import VERSION, SilverError, transform


@dataclass
class RunResult:
    hour: str
    skipped: bool = False
    bronze_rows: int = 0
    silver_rows: int = 0
    g2_status: str = ""                         # "pass" | "warn" (o "fail" levanta GateFailed)
    bronze_commit: str = ""                     # o commit de onde se leu o bronze (ADR-019, D8)


class GateFailed(SilverError):
    """O G2 encontrou um problema `blocker`: a silver ficou no branch, mas não se junta ao destino."""


def partition(hour: datetime) -> tuple[str, str]:
    hour = hour.astimezone(timezone.utc)
    return hour.strftime("%Y-%m-%d"), hour.strftime("%H")


def run_hour(lake, hour: datetime, *, target: str = "main", hook=None, keep_branch: bool = False) -> RunResult:
    """Processa uma hora. Cada passo é idempotente: repetir a hora dá o mesmo resultado, e se algo falhar
    a meio o `target` fica intacto (o que ficou por acabar fica no branch)."""
    hook = hook or (lambda step: None)          # os testes usam-no para simular uma falha em qualquer passo
    dt, hh = partition(hour)
    # T1 (ADR-019, D8): fixa-se o commit do `target` ANTES de listar. O branch `main` avança enquanto lemos (o writer
    # junta lotes de 15 em 15 min); lendo tudo de um ID de commit, a lista e os ficheiros vêm da mesma versão do lake,
    # e o ID fica guardado como prova de que bronze originou esta silver.
    bronze_commit = lake.resolve(target)
    bronze_files = [p for p in lake.list(bronze_commit, f"bronze/dt={dt}/hour={hh}/") if p.endswith(".parquet")]
    if not bronze_files:
        return RunResult(f"{dt}T{hh}", skipped=True)

    branch, silver_path = f"silver-{dt}-{hh}", f"silver/dt={dt}/hour={hh}/data.parquet"
    lake.ensure_branch(branch, target)
    hook("branch")
    with tempfile.TemporaryDirectory() as tmp:
        local = []
        for i, path in enumerate(bronze_files):
            local.append(Path(tmp) / f"bronze{i}.parquet")
            # lido do commit fixo (onde foi listado), não do branch: um branch silver-* que ficou de uma corrida falhada
            # é anterior ao bronze que chegou depois, e não teria esses ficheiros (auditoria de 2026-11-17)
            local[-1].write_bytes(lake.read(bronze_commit, path))
        out = Path(tmp) / "silver.parquet"
        catalog = load_catalog()
        result = transform(local, out, catalog)                 # levanta SilverError se o bronze não cumprir o contrato
        hook("transform")
        lake.upload(branch, silver_path, out.read_bytes())
        hook("upload")
        report = g2.evaluate(out, hour, catalog)                # o gate lê o ficheiro que acabámos de gerar
    attested = {silver_path: lake.checksum(branch, silver_path)}
    gate_path = f"_gates/g2/dt={dt}/hour={hh}.json"
    bronze_commit = _keep_previous_bronze_commit(lake, target, gate_path, attested, bronze_commit)
    lake.upload(branch, gate_path, report.attestation(attested, bronze_commit))   # a prova que o hook do lakeFS confere
    hook("g2")
    openmetadata.publish(hour, report)      # o histórico do G2 no catálogo (melhor esforço: nunca falha a hora)

    lake.commit(branch, f"silver: {dt} hora {hh}", {"bronze_files": str(len(bronze_files)), "bronze_rows": str(result.bronze_rows),
                                                  "silver_rows": str(result.silver_rows), "silver_version": VERSION,
                                                  "bronze_commit": bronze_commit})
    hook("commit")
    stored = pq.ParquetFile(io.BytesIO(lake.read(branch, silver_path))).metadata.num_rows        # o que está no lakeFS é o que calculámos?
    if stored != result.bronze_rows:
        raise SilverError(f"no lakeFS há {stored} linhas de silver e o bronze tem {result.bronze_rows}")
    hook("verify")

    if report.status == "fail":                                 # o hook do lakeFS recusaria de qualquer forma; aqui falha-se cedo e alto
        raise GateFailed(f"G2 falhou para {dt}T{hh}: {report.summary}. O branch {branch} fica por juntar para autópsia.")
    lake.merge(branch, target, f"silver: {dt} hora {hh}")
    hook("merge")
    if not keep_branch:
        lake.delete_branch(branch)
    return RunResult(f"{dt}T{hh}", False, result.bronze_rows, result.silver_rows, report.status, bronze_commit)


def _keep_previous_bronze_commit(lake, target: str, gate_path: str, attested: dict[str, str], bronze_commit: str) -> str:
    """Repetir uma hora tem de dar os mesmos bytes (e nenhum commit novo). Mas o `main` avançou desde a 1.ª vez, por isso
    o commit resolvido agora é outro. Se a atestação que já está no `target` validou exatamente a mesma silver (mesmo
    checksum), fica o commit de bronze que ela regista: a silver tem uma linha por linha de bronze, com o
    `bronze_event_id`, o lote e o valor, logo a mesma silver só pode ter vindo do mesmo bronze, e esse commit continua a
    existir (os commits do lakeFS não se apagam). Se a silver mudou, ou a prova antiga não tinha o campo (silver anterior
    ao T1), fica o commit novo."""
    try:
        previous = json.loads(lake.read(target, gate_path))
    except Exception:                     # ainda não há prova no destino (1.ª vez). Noutro erro, o pior é um commit a mais
        return bronze_commit
    if previous.get("files") == attested and previous.get("bronze_commit"):
        return previous["bronze_commit"]
    return bronze_commit
