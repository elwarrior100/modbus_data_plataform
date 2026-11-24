"""Retenção da silver: 3 meses, e só depois de o gold desse dia estar numa release (ADR-020, decisão 3).

O gold guarda para sempre os números ao minuto e à hora; a silver (cada leitura ao segundo) pode sair ao fim de 3 meses.
**Regra de segurança:** um dia D só sai se houver uma release do gold de D **e** de D+1 com o G3 aprovado (o gold de
D+1 lê a hora 23 de D, por isso D só deixa de fazer falta quando D+1 também estiver fechado). Sem release, o dia fica,
por mais antigo que seja: perder a silver de um dia sem gold seria perder esse dia de vez.

Como o bronze (`silver.retention.expire_bronze`): sai do `main` num branch `retention-silver-<data>`, com commit e merge
(o `main` está protegido). O hook do G2 só olha para silver acrescentada ou alterada, por isso uma remoção passa. As
atestações dos gates (`_gates/`) ficam: são poucos KB e são a prova do que existiu. O espaço só volta na GC seguinte
(o lakeFS apaga os ficheiros 15 dias depois). Primeiro dia elegível: ~2027-02-23 (o 1.º gold é de 2026-11-23).
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .release import read_manifest, releases

KEEP_SILVER_DAYS = 92                # ~3 meses (ADR-020, decisão 3)
SILVER_DAY = re.compile(r"^silver/dt=(\d{4}-\d{2}-\d{2})/$")


@dataclass
class SilverReport:
    expired: list[str] = field(default_factory=list)        # dias que saem (ou sairiam, em simulação)
    waiting: list[str] = field(default_factory=list)        # dias antigos que ficam: sem release de D ou de D+1
    kept: int = 0                                           # dias dentro dos 3 meses
    objects: int = 0
    branch: str | None = None
    merged: bool = False


def released_days(lake) -> set[str]:
    """Os dias com uma release cujo G3 não falhou (pelos manifests das tags gold-v*)."""
    days = set()
    for tag, _ in releases(lake):
        m = read_manifest(lake, tag)
        if m.get("gates", {}).get("g3") in ("pass", "warn"):
            days.add(m["covers"]["day"])
    return days


def expire_silver(lake, today: date, *, keep_days: int = KEEP_SILVER_DAYS, dry_run: bool = True,
                  into: str = "main") -> SilverReport:
    """Tira de `into` a silver com mais de `keep_days` dias, só dos dias com release (de D e de D+1)."""
    if keep_days < 30:
        raise ValueError("keep_days abaixo de 30: o G3 e os reprocessamentos precisam da silver recente")
    cutoff = today - timedelta(days=keep_days)
    released = released_days(lake)
    report = SilverReport()
    for prefix in lake.partitions(into, "silver/"):
        m = SILVER_DAY.match(prefix)
        if not m:
            continue                                   # outra coisa dentro de silver/: não é nossa
        day = date.fromisoformat(m.group(1))
        if day >= cutoff:
            report.kept += 1
        elif day.isoformat() in released and (day + timedelta(days=1)).isoformat() in released:
            report.expired.append(day.isoformat())
        else:
            report.waiting.append(day.isoformat())
    if not report.expired:
        return report
    report.branch = f"retention-silver-{today:%Y%m%d}" if into == "main" else f"{into}-retention-silver"
    paths = [p for d in report.expired for p in lake.list(into, f"silver/dt={d}/")]
    report.objects = len(paths)
    if dry_run:
        return report
    lake.ensure_branch(report.branch, into)
    lake.delete(report.branch, [p for d in report.expired for p in lake.list(report.branch, f"silver/dt={d}/")])
    lake.commit(report.branch, f"retenção: silver anterior a {cutoff} ({len(report.expired)} dias, com release)",
                {"retention": "silver", "keep_days": str(keep_days), "expired": ",".join(report.expired)})
    report.merged = lake.merge(report.branch, into, f"retenção: silver anterior a {cutoff}")
    lake.delete_branch(report.branch)
    return report


if __name__ == "__main__":
    import sys
    from datetime import datetime, timezone

    from silver.lake import Lake

    apply = "--apply" in sys.argv
    r = expire_silver(Lake.from_env(), datetime.now(timezone.utc).date(), dry_run=not apply)
    print(json.dumps({"applied": apply, "expired": r.expired, "objects": r.objects, "waiting_for_release": r.waiting,
                      "kept_days": r.kept, "merged": r.merged}, indent=1))
