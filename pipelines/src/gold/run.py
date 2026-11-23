"""Uma execução do gold: um dia UTC de silver → gold, num branch, e só então para o main (como o `silver.run.run_hour`).

    resolve o main (commit fixo) → branch gold-D → lê a silver da janela e o gold de D−1 → build → upload → G3 →
    atestação → commit → verifica → merge → apaga o branch

Cada passo é idempotente: repetir o dia dá os mesmos bytes e nenhum commit novo; se algo falhar a meio, o `main` fica
intacto (o que ficou por acabar fica no branch). A release do dia (tag + manifest + `LATEST_STABLE`) faz-se a seguir,
em `gold.release.release_day` (a DAG chama os dois).
"""
import json
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from silver.run import GateFailed
from silver.transform import SilverError

from . import g3
from .build import GOLD_VERSION, TABLES, build_day, gold_path, silver_window

UTC = timezone.utc
HISTORY_DAYS = 7                     # G3-05 e G3-06 comparam com até 7 dias anteriores do próprio gold
UNKNOWN_BRONZE = "desconhecido (silver anterior a ADR-019 T1)"


@dataclass
class DayResult:
    day: str
    skipped: bool = False
    g3_status: str = ""                          # "pass" | "warn" (o "fail" levanta GateFailed)
    samples: int = 0                             # amostras da silver no dia
    rows: dict[str, int] = field(default_factory=dict)
    silver_commit: str = ""


def _silver_path(hour: datetime) -> str:
    return f"silver/dt={hour:%Y-%m-%d}/hour={hour:%H}/data.parquet"


def _inputs(lake, commit: str, paths: list[str]) -> dict:
    """De onde vem o gold: o commit da silver e, por partição, o checksum, o G2 e o commit de bronze (ADR-019, D8)."""
    parts = {}
    for path in paths:
        dt, hour = path.split("/")[1], path.split("/")[2]
        try:
            att = json.loads(lake.read(commit, f"_gates/g2/{dt}/{hour}.json"))
        except Exception:                        # silver anterior aos gates: não há prova
            att = {}
        parts[path] = {"checksum": lake.checksum(commit, path), "g2": att.get("status", "desconhecido"),
                       "bronze_commit": att.get("bronze_commit", UNKNOWN_BRONZE)}
    return {"silver_commit": commit, "silver_partitions": parts}


def _keep_previous_inputs(lake, target: str, gate_path: str, attested: dict, inputs: dict) -> dict:
    """Repetir o dia não pode criar um commit só porque o `main` avançou (o mesmo problema do T1 na silver): se a prova
    que já está no destino validou os mesmos ficheiros gold a partir das mesmas partições de silver, fica o commit de
    silver que ela regista (continua a existir e tem exatamente essa silver)."""
    try:
        previous = json.loads(lake.read(target, gate_path))
    except Exception:
        return inputs
    old = previous.get("inputs", {})
    if previous.get("files") == attested and old.get("silver_partitions") == inputs["silver_partitions"]:
        return old
    return inputs


def run_day(lake, day: date, *, target: str = "main", hook=None, keep_branch: bool = False) -> DayResult:
    hook = hook or (lambda step: None)          # os testes usam-no para simular uma falha em qualquer passo
    # tudo se lê de um commit fixo: a silver de várias horas e o gold de D−1 vêm da mesma versão do lake
    commit = lake.resolve(target)
    present = set()
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        present.update(p for p in lake.list(commit, f"silver/dt={d.isoformat()}/") if p.endswith("/data.parquet"))
    window = [h for h in silver_window(day) if _silver_path(h) in present]
    silver_hours = {h.hour for h in window if h.date() == day}
    if not silver_hours:                         # nenhuma hora de silver no dia: não há gold a fazer
        return DayResult(day.isoformat(), skipped=True, silver_commit=commit)

    gold_tables = set(lake.list(commit, "gold/fact_tag_hour/")) | set(lake.list(commit, "gold/fact_asset_hour/"))
    yesterday = day - timedelta(days=1)
    history = [gold_path("fact_tag_hour", day - timedelta(days=i)) for i in range(HISTORY_DAYS, 0, -1)]
    history = [p for p in history if p in gold_tables]

    branch = f"gold-{day.isoformat()}"
    gate_path = f"_gates/g3/dt={day.isoformat()}.json"
    lake.ensure_branch(branch, target)
    hook("branch")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        def fetch(path: str) -> Path:
            local = tmp / "in" / path
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(lake.read(commit, path))
            return local

        silver = [fetch(_silver_path(h)) for h in window]
        previous = {t: fetch(gold_path(t, yesterday)) for t in ("fact_tag_hour", "fact_asset_hour")
                    if gold_path(t, yesterday) in gold_tables}
        result = build_day(silver, day, tmp / "out", previous_tag_hour=previous.get("fact_tag_hour"),
                           previous_asset_hour=previous.get("fact_asset_hour"))
        hook("build")
        for path, local in sorted(result.files.items()):
            lake.upload(branch, path, local.read_bytes())
        hook("upload")
        report = g3.evaluate(day, result.files, silver, silver_hours=silver_hours,
                             history=[fetch(p) for p in history])
    attested = {path: lake.checksum(branch, path) for path in sorted(result.files)}
    inputs = _keep_previous_inputs(lake, target, gate_path, attested, _inputs(lake, commit, [_silver_path(h) for h in window]))
    lake.upload(branch, gate_path, report.attestation(attested, inputs))       # a prova que o hook g3_pre_merge confere
    hook("g3")

    lake.commit(branch, f"gold: {day.isoformat()}", {"gold_version": GOLD_VERSION, "silver_commit": inputs["silver_commit"],
                                                     "g3": report.status, "samples": str(report.rows)})
    hook("commit")
    for path in sorted(attested):                # o que está no lakeFS é o que o G3 validou?
        if lake.checksum(branch, path) != attested[path]:
            raise SilverError(f"no lakeFS, {path} não é o ficheiro que o G3 validou")
    if set(attested) != {gold_path(t, day) for t in TABLES}:
        raise SilverError(f"o gold de {day} não tem as {len(TABLES)} tabelas")
    hook("verify")

    if report.status == "fail":                  # o hook do lakeFS recusaria de qualquer forma; aqui falha-se cedo e alto
        raise GateFailed(f"G3 falhou para {day}: {report.summary}. O branch {branch} fica por juntar para autópsia.")
    lake.merge(branch, target, f"gold: {day.isoformat()}")
    hook("merge")
    if not keep_branch:
        lake.delete_branch(branch)
    return DayResult(day.isoformat(), False, report.status, report.rows, result.rows, inputs["silver_commit"])
