from silver.retention import DAY, prune

NOW = 1_000_000_000.0


class Stub:
    def __init__(self, branches, main_history):
        self.branches, self.main_history, self.deleted = branches, set(main_history), []

    def branch_infos(self):
        return self.branches

    def history_ids(self, ref="main"):
        return self.main_history

    def delete_branch(self, name):
        self.deleted.append(name)


def age(days):
    return NOW - days * DAY


def lake():
    return Stub(
        [("main", "m", age(0)),
         ("etl-0123456789abcdef", "c1", age(8)),       # juntado, velho -> apaga
         ("etl-aaaaaaaaaaaaaaaa", "c2", age(2)),       # juntado, recente -> fica
         ("etl-bbbbbbbbbbbbbbbb", "x1", age(10)),      # por juntar, dentro dos 30 dias -> fica
         ("etl-cccccccccccccccc", "x2", age(31)),      # por juntar, passou os 30 dias -> apaga
         ("silver-2026-11-07-17", "c3", age(9)),       # juntado, velho -> apaga
         ("etl-gap", "g", age(400)),                   # manual: nunca se toca
         ("minha-experiencia", "e", age(400))],        # manual: nunca se toca
        main_history={"m", "c1", "c2", "c3"})


def test_the_policy_deletes_only_old_pipeline_branches():
    report = prune(lake(), NOW, dry_run=False)
    assert sorted(report.delete) == ["etl-0123456789abcdef", "etl-cccccccccccccccc", "silver-2026-11-07-17"]
    assert (report.keep_merged, report.keep_unmerged, report.ignored) == (1, 1, 3)


def test_dry_run_deletes_nothing():
    stub = lake()
    report = prune(stub, NOW)                                   # simulação é o comportamento por omissão
    assert len(report.delete) == 3 and stub.deleted == []


def test_real_run_deletes_exactly_what_the_report_says():
    stub = lake()
    report = prune(stub, NOW, dry_run=False)
    assert sorted(stub.deleted) == sorted(report.delete)


# --- bronze crua (ADR-015) ---
from datetime import date

import pytest

from fake_lake import FakeLake
from silver.retention import expire_bronze

TODAY = date(2026, 12, 1)


def bronze_lake():
    lake = FakeLake()
    lake.load_bronze({
        "bronze/dt=2026-11-14/hour=10/a.parquet": b"a",      # 17 dias: sai
        "bronze/dt=2026-11-14/hour=11/b.parquet": b"b",      # 17 dias: sai
        "bronze/dt=2026-11-15/hour=00/c.parquet": b"c",      # 16 dias: sai
        "bronze/dt=2026-11-16/hour=00/d.parquet": b"d",      # 15 dias (= hoje - 15): fica
        "bronze/dt=2026-11-30/hour=23/e.parquet": b"e",      # ontem: fica
        "bronze/notas.txt": b"x",                             # fora do padrão dt=: nunca se toca
        "silver/dt=2026-11-14/hour=10/data.parquet": b"s",    # silver: fica para sempre
        "_gates/g2/dt=2026-11-14/hour=10.json": b"g",         # atestações: ficam para sempre
    })
    return lake


def test_bronze_older_than_15_days_leaves_main_and_nothing_else_does():
    lake = bronze_lake()
    report = expire_bronze(lake, TODAY, dry_run=False)
    assert report.expired == ["2026-11-14", "2026-11-15"] and report.objects == 3 and report.kept == 2
    assert report.branch == "retention-20261201" and report.merged
    assert lake.list("main", "bronze/") == ["bronze/dt=2026-11-16/hour=00/d.parquet",
                                            "bronze/dt=2026-11-30/hour=23/e.parquet", "bronze/notas.txt"]
    assert lake.list("main", "silver/") and lake.list("main", "_gates/")


def test_bronze_dry_run_changes_nothing():
    lake = bronze_lake()
    before = lake.list("main", "")
    report = expire_bronze(lake, TODAY)                        # simulação é o comportamento por omissão
    assert report.objects == 3 and not report.merged and lake.list("main", "") == before
    assert "retention-20261201" not in lake.branches


def test_bronze_second_run_the_same_day_does_nothing():
    lake = bronze_lake()
    expire_bronze(lake, TODAY, dry_run=False)
    again = expire_bronze(lake, TODAY, dry_run=False)
    assert again.expired == [] and again.branch is None


def test_bronze_written_on_main_meanwhile_survives_the_merge():
    lake = bronze_lake()
    lake.ensure_branch("retention-20261201")
    lake.load_bronze({"bronze/dt=2026-12-01/hour=00/new.parquet": b"n"})   # chega ao main depois do branch
    expire_bronze(lake, TODAY, dry_run=False)
    assert "bronze/dt=2026-12-01/hour=00/new.parquet" in lake.list("main", "bronze/")


def test_bronze_keep_days_below_one_is_refused():
    with pytest.raises(ValueError):
        expire_bronze(bronze_lake(), TODAY, keep_days=0)


def test_retention_branches_are_pruned_like_the_others():
    stub = Stub([("retention-20261101", "r1", age(8))], main_history={"r1"})
    assert prune(stub, NOW).delete == ["retention-20261101"]
