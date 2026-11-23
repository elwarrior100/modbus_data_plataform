"""Políticas de retenção do lake (ADR-007 e ADR-015).

Branches de trabalho (`prune`):

- juntado ao main (o commit da ponta está na história do main) e com mais de `merged_days`: apaga-se;
- por juntar (lote rejeitado, ou silver que falhou) e com mais de `unmerged_days`: apaga-se também;
  até lá ficam para autópsia;
- só se tocam nomes que o pipeline cria: `etl-<hex>`, `silver-<data>-<hora>`, `retention-<data>` e
  `retention-silver-<data>` (a retenção da silver, `gold/retention.py`).
  O main e branches manuais (ex.: `etl-gap`) nunca se tocam.

Bronze crua (`expire_bronze`): as partições `bronze/dt=D/` com D anterior a hoje - `keep_days` saem do main por um
branch `retention-<data>` (commit + merge: o main está protegido). Nunca toca na silver nem nas atestações dos gates
(`_gates/`), que ficam para sempre. Apagar no main não liberta espaço sozinho: a garbage collection do lakeFS
(plataforma) é que remove os objetos que nenhum commit dentro do prazo ainda referencia.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

OURS = re.compile(r"^(etl-[0-9a-f]{8,}|silver-\d{4}-\d{2}-\d{2}-\d{2}|retention-\d{8}|retention-silver-\d{8})$")
BRONZE_DAY = re.compile(r"^bronze/dt=(\d{4}-\d{2}-\d{2})/$")
KEEP_BRONZE_DAYS = 15
DAY = 86400


@dataclass
class Report:
    delete: list[str] = field(default_factory=list)
    keep_merged: int = 0       # juntados mas ainda dentro do prazo
    keep_unmerged: int = 0     # por juntar, guardados para autópsia
    ignored: int = 0           # nomes que não são do pipeline


def prune(lake, now: float, *, merged_days: int = 7, unmerged_days: int = 30, dry_run: bool = True) -> Report:
    report = Report()
    history = lake.history_ids("main")
    for name, head, created in lake.branch_infos():
        if not OURS.match(name):
            report.ignored += 1
            continue
        age_days = (now - created) / DAY
        merged = head in history
        if age_days > (merged_days if merged else unmerged_days):
            report.delete.append(name)
        elif merged:
            report.keep_merged += 1
        else:
            report.keep_unmerged += 1
    if not dry_run:
        for name in report.delete:
            lake.delete_branch(name)
    return report


@dataclass
class BronzeReport:
    expired: list[str] = field(default_factory=list)   # datas (AAAA-MM-DD) que saem
    objects: int = 0                                   # objetos apagados (ou a apagar, em simulação)
    kept: int = 0                                      # partições que ficam
    branch: str | None = None
    merged: bool = False


def expire_bronze(lake, today: date, *, keep_days: int = KEEP_BRONZE_DAYS, dry_run: bool = True,
                  into: str = "main") -> BronzeReport:
    """Tira de `into` a bronze com mais de `keep_days` dias. Repetir no mesmo dia não faz mal."""
    if keep_days < 1:
        raise ValueError("keep_days tem de ser pelo menos 1 (o dia de hoje está a ser escrito)")
    cutoff = today - timedelta(days=keep_days)
    report = BronzeReport()
    for prefix in lake.partitions(into, "bronze/"):
        m = BRONZE_DAY.match(prefix)
        if not m:
            continue                                   # outra coisa dentro de bronze/: não é nossa
        if date.fromisoformat(m.group(1)) < cutoff:
            report.expired.append(m.group(1))
        else:
            report.kept += 1
    if not report.expired:
        return report
    report.branch = f"retention-{today:%Y%m%d}" if into == "main" else f"{into}-retention"   # testes: nunca o main
    paths = [p for d in report.expired for p in lake.list(into, f"bronze/dt={d}/")]
    report.objects = len(paths)
    if dry_run:
        return report
    lake.ensure_branch(report.branch, into)
    lake.delete(report.branch, [p for d in report.expired for p in lake.list(report.branch, f"bronze/dt={d}/")])
    lake.commit(report.branch, f"retenção: bronze anterior a {cutoff} ({len(report.expired)} dias)",
                {"retention": "bronze", "keep_days": str(keep_days), "expired": ",".join(report.expired)})
    report.merged = lake.merge(report.branch, into, f"retenção: bronze anterior a {cutoff}")
    return report


if __name__ == "__main__":
    import sys
    import time

    from .lake import Lake

    apply = "--apply" in sys.argv
    result = prune(Lake.from_env(), time.time(), dry_run=not apply)
    print(f"{'APAGADOS' if apply else 'APAGARIA (simulação)'}: {len(result.delete)} | "
          f"juntados a guardar: {result.keep_merged} | por juntar a guardar: {result.keep_unmerged} | "
          f"ignorados (não são do pipeline): {result.ignored}")
    for name in result.delete[:10]:
        print("  ", name)
