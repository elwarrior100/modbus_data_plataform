"""O pipeline da silver contra o lakeFS REAL (precisa de LAKEFS_ENDPOINT e das chaves no ambiente).

Nunca toca no main: cria branches de destino descartáveis a partir do main, carrega lá bronze sintético
(de 2026-11-03, que não existe no main real) e corre o `run_hour` com `target=<descartável>`.
No fim apaga só os branches que o próprio teste criou."""
import os
import time
from datetime import datetime, timezone

import pytest

from silver.lake import Lake
from silver.run import run_hour

pytestmark = [pytest.mark.lakefs, pytest.mark.skipif("LAKEFS_ENDPOINT" not in os.environ, reason="sem lakeFS")]

HOURS = [datetime(2026, 11, 3, h, tzinfo=timezone.utc) for h in range(0, 6)]


@pytest.fixture
def lake():
    lake = Lake.from_env()
    existing = {b.id for b in lake.repo.branches()}            # o que já lá estava nunca é tocado
    yield lake
    for branch in {b.id for b in lake.repo.branches()} - existing:
        lake.delete_branch(branch)


def new_target(lake, week, name):
    """Um branch descartável com o bronze das 6 horas já commitado."""
    target = f"silver-test-{name}-{int(time.time() * 1000)}"
    lake.ensure_branch(target, "main")
    for path, data in week[1].items():
        if "dt=2026-11-03" in path and any(f"hour={h.hour:02d}/" in path for h in HOURS):
            lake.upload(target, path, data)
    assert lake.commit(target, "bronze sintético de teste", {})
    return target


def silver_of(lake, target):
    """Só a silver do dia de teste: o branch sai do main real, que já tem a silver de 2026-11-07."""
    return {p: lake.read(target, p) for p in lake.list(target, "silver/dt=2026-11-03/")}


def head(lake, branch):
    return lake.repo.branch(branch).get_commit().id


def test_ascending_and_descending_give_identical_silver(lake, week):
    a, b = new_target(lake, week, "a"), new_target(lake, week, "b")
    for hour in HOURS:
        run_hour(lake, hour, target=a)
    for hour in reversed(HOURS):
        run_hour(lake, hour, target=b)
    silver_a, silver_b = silver_of(lake, a), silver_of(lake, b)
    assert len(silver_a) == len(HOURS) and silver_a == silver_b      # mesmos ficheiros, mesmos bytes


def test_rerunning_an_hour_adds_no_commit(lake, week):
    t = new_target(lake, week, "idem")
    run_hour(lake, HOURS[2], target=t)
    before, silver = head(lake, t), silver_of(lake, t)
    run_hour(lake, HOURS[2], target=t)
    assert head(lake, t) == before and silver_of(lake, t) == silver


@pytest.mark.parametrize("step", ["branch", "transform", "upload", "commit", "verify"])
def test_a_crash_before_the_merge_leaves_the_target_intact_then_the_rerun_finishes(lake, week, step):
    t = new_target(lake, week, f"crash-{step}")
    before = head(lake, t)

    def die(name):
        if name == step:
            raise RuntimeError(f"morreu em {name}")

    with pytest.raises(RuntimeError):
        run_hour(lake, HOURS[3], target=t, hook=die)
    assert head(lake, t) == before and silver_of(lake, t) == {}       # o destino não viu nada
    result = run_hour(lake, HOURS[3], target=t)
    assert result.silver_rows == result.bronze_rows > 0 and len(silver_of(lake, t)) == 1


def test_main_is_never_touched(lake, week):
    main_before = head(lake, "main")
    t = new_target(lake, week, "main")
    run_hour(lake, HOURS[0], target=t)
    assert head(lake, "main") == main_before
