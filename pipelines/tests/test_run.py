"""Uma execução (uma hora) contra um lakeFS em memória: o ciclo, a idempotência e as falhas a meio."""
from datetime import datetime, timezone

import pyarrow.parquet as pq
import pytest

from fake_lake import FakeLake
from silver.run import run_hour
from silver.transform import SilverError

import io

H = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
SILVER = "silver/dt=2026-11-03/hour=09/data.parquet"
BRONZE = "bronze/dt=2026-11-03/hour=09/"


@pytest.fixture
def lake(week):
    lake = FakeLake()
    lake.load_bronze({p: d for p, d in week[1].items() if "dt=2026-11-03" in p and "hour=0" in p})   # as horas 00 a 09 desse dia
    return lake


def bronze_rows(lake):
    return sum(pq.ParquetFile(io.BytesIO(lake.read("main", p))).metadata.num_rows for p in lake.list("main", BRONZE))


def test_one_hour_becomes_one_silver_partition_with_the_same_number_of_rows(lake):
    result = run_hour(lake, H)
    assert not result.skipped and result.bronze_rows == result.silver_rows == bronze_rows(lake) > 0
    assert list(lake.silver()) == [SILVER]
    assert pq.ParquetFile(io.BytesIO(lake.silver()[SILVER])).metadata.num_rows == result.silver_rows
    assert set(lake.branches) == {"main"}                                  # o branch de trabalho foi apagado depois do merge


def test_running_the_same_hour_twice_changes_nothing(lake):
    run_hour(lake, H)
    first, commits = dict(lake.silver()), lake.commits["main"]
    again = run_hour(lake, H)
    assert lake.silver() == first                                          # mesmos bytes
    assert lake.commits["main"] == commits                                 # e nenhum commit novo no main
    assert again.silver_rows == bronze_rows(lake)


def test_an_hour_without_bronze_is_skipped_and_creates_nothing(lake):
    result = run_hour(lake, datetime(2026, 11, 3, 23, tzinfo=timezone.utc))
    assert result.skipped and lake.silver() == {} and set(lake.branches) == {"main"}


@pytest.mark.parametrize("step", ["branch", "transform", "upload", "commit", "verify"])
def test_a_failure_before_the_merge_never_touches_main_and_the_rerun_finishes(lake, step):
    def die(name):
        if name == step:
            raise RuntimeError(f"morreu em {name}")

    main_before = dict(lake.branches["main"]["committed"])
    with pytest.raises(RuntimeError):
        run_hour(lake, H, hook=die)
    assert lake.branches["main"]["committed"] == main_before              # o main ficou exatamente como estava
    assert any(b.startswith("silver-") for b in lake.branches)            # o trabalho por acabar ficou no branch
    result = run_hour(lake, H)                                            # retomar = fazer outra vez
    assert result.silver_rows == bronze_rows(lake) and list(lake.silver()) == [SILVER]
    assert set(lake.branches) == {"main"}


def test_a_failure_right_after_the_merge_is_harmless_on_the_rerun(lake):
    def die(name):
        if name == "merge":
            raise RuntimeError("morreu depois do merge")

    with pytest.raises(RuntimeError):
        run_hour(lake, H, hook=die)
    assert list(lake.silver()) == [SILVER]                                # o merge já tinha acontecido
    commits = lake.commits["main"]
    run_hour(lake, H)
    assert lake.commits["main"] == commits and set(lake.branches) == {"main"}


def test_bronze_that_breaks_the_contract_fails_and_main_stays_clean(week):
    from fixture_bronze import bronze_files
    from ingest.envelope import Event

    now = 1_794_000_000_000_000
    bad = [Event.build("ot-gateway", "ns=2;s=INVENTADO.x", "1", 0, now, now, now)]
    lake = FakeLake()
    lake.load_bronze(bronze_files(bad))
    hour = datetime.fromtimestamp(now / 1e6, timezone.utc)
    with pytest.raises(SilverError, match="catálogo"):
        run_hour(lake, hour)
    assert lake.silver() == {}


# --- T1 (ADR-019, D8): a silver regista de que commit de bronze leu ---------------------------------------------

def _attestation(lake, ref="main"):
    import json
    return json.loads(lake.read(ref, "_gates/g2/dt=2026-11-03/hour=09.json"))


def test_the_silver_records_the_bronze_commit_it_read(lake):
    expected = lake.resolve("main")                                        # o main antes da corrida
    result = run_hour(lake, H)
    assert result.bronze_commit == expected
    assert _attestation(lake)["bronze_commit"] == expected                  # na prova do G2…
    assert lake.commit_metadata[("silver-2026-11-03-09", "silver: 2026-11-03 hora 09")]["bronze_commit"] == expected  # …e no commit
    assert lake.list(expected, BRONZE) == lake.list("main", BRONZE)        # e esse commit tem o bronze que se leu


def test_bronze_that_arrives_while_the_hour_runs_is_not_mixed_in(lake, week):
    """O main avança a meio (um lote do writer chega depois de listar): lê-se tudo do commit fixo, nada a meias."""
    late = "bronze/dt=2026-11-03/hour=09/zz-lote-tardio.parquet"
    extra = next(d for p, d in week[1].items() if "dt=2026-11-03/hour=08/" in p)

    def writer_merges(step):
        if step == "branch":
            lake.branches["main"]["committed"][late] = extra               # um commit novo no main
    before = bronze_rows(lake)
    result = run_hour(lake, H, hook=writer_merges)
    assert result.bronze_rows == before                                    # só o bronze do commit fixo
    assert late not in lake.list(result.bronze_commit, BRONZE)
    again = run_hour(lake, H)                                              # a corrida seguinte apanha o lote tardio
    assert again.bronze_rows > before and again.bronze_commit != result.bronze_commit
    assert _attestation(lake)["bronze_commit"] == again.bronze_commit


def test_repeating_an_hour_keeps_the_first_bronze_commit_even_after_main_moved(lake, week):
    first = run_hour(lake, H)
    commits = lake.commits["main"]
    lake.branches["main"]["committed"]["outra/coisa.txt"] = b"o main avancou"   # outro commit, nada a ver com esta hora
    assert lake.resolve("main") != first.bronze_commit
    again = run_hour(lake, H)
    assert again.bronze_commit == first.bronze_commit                     # a mesma silver: a mesma origem
    assert lake.commits["main"] == commits                                # e nenhum commit novo (mesmos bytes)
