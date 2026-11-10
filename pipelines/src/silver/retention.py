"""Política de retenção dos branches de trabalho do pipeline (ADR-007).

- juntado ao main (o commit da ponta está na história do main) e com mais de `merged_days`: apaga-se;
- por juntar (lote rejeitado, ou silver que falhou) e com mais de `unmerged_days`: apaga-se também;
  até lá ficam para autópsia;
- só se tocam nomes que o pipeline cria: `etl-<hex>` e `silver-<data>-<hora>`.
  O main e branches manuais (ex.: `etl-gap`) nunca se tocam.
"""
import re
from dataclasses import dataclass, field

OURS = re.compile(r"^(etl-[0-9a-f]{8,}|silver-\d{4}-\d{2}-\d{2}-\d{2})$")
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
