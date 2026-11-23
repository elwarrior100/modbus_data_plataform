"""Retenção da silver (ADR-020, decisão 3): 3 meses, e só dos dias com release do gold de D e de D+1."""
import json
from datetime import date

import pytest

from fake_lake import FakeLake
from gold.retention import expire_silver

TODAY = date(2027, 3, 1)                     # 92 dias antes: 2026-11-29


def lake_with(days, released):
    lake = FakeLake()
    for d in days:
        for h in ("00", "23"):
            lake.upload("main", f"silver/dt={d}/hour={h}/data.parquet", f"{d}{h}".encode())
        lake.upload("main", f"_gates/g2/dt={d}/hour=00.json", b"{}")
    for d in released:
        tag = f"gold-v1.0.0_{d.replace('-', '')}0240"
        lake.upload("main", f"_releases/{tag}/manifest.json",
                    json.dumps({"release": tag, "covers": {"day": d}, "gates": {"g3": "pass"}}).encode())
    lake.commit("main", "dados", {})
    for d in released:
        tag = f"gold-v1.0.0_{d.replace('-', '')}0240"
        lake.create_tag(tag, "main")
    return lake


def test_only_old_days_with_a_release_of_that_day_and_the_next_leave():
    days = ["2026-11-25", "2026-11-26", "2026-11-27", "2026-11-28", "2026-12-15"]
    lake = lake_with(days, released=["2026-11-25", "2026-11-26", "2026-11-28"])
    report = expire_silver(lake, TODAY, dry_run=False, into="main")
    assert report.expired == ["2026-11-25"]                      # 26 tem release, mas o 27 não: fica
    assert report.waiting == ["2026-11-26", "2026-11-27", "2026-11-28"] and report.kept == 1
    left = lake.list("main", "silver/")
    assert not any("2026-11-25" in p for p in left) and any("2026-11-26" in p for p in left)
    assert lake.list("main", "_gates/g2/dt=2026-11-25/")           # as provas ficam
    assert not any(b.startswith("main-retention") for b in lake.branches)     # o branch de trabalho foi apagado


def test_simulation_is_the_default_and_repeating_is_harmless():
    lake = lake_with(["2026-11-25", "2026-11-26"], released=["2026-11-25", "2026-11-26"])
    assert expire_silver(lake, TODAY).expired == ["2026-11-25"]     # simulação: nada sai
    assert any("2026-11-25" in p for p in lake.list("main", "silver/"))
    expire_silver(lake, TODAY, dry_run=False)
    again = expire_silver(lake, TODAY, dry_run=False)
    assert again.expired == [] and again.branch is None


def test_a_failed_g3_does_not_count_as_a_release():
    lake = lake_with(["2026-11-25", "2026-11-26"], released=["2026-11-25", "2026-11-26"])
    tag = "gold-v1.0.0_202611250240"
    m = json.loads(lake.read(tag, f"_releases/{tag}/manifest.json"))
    assert m["gates"]["g3"] == "pass"
    lake2 = lake_with(["2026-11-25", "2026-11-26"], released=["2026-11-26"])
    assert expire_silver(lake2, TODAY).expired == []                  # sem release de 25: fica


def test_keep_days_has_a_floor():
    with pytest.raises(ValueError):
        expire_silver(FakeLake(), TODAY, keep_days=7)
