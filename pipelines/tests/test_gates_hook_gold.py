"""Os hooks do gold (G3 e tags de release) no lakeFS REAL, num repositório descartável criado só para o teste.
NÃO corridos ainda (2026-11-23: sem lakeFS à mão nesta sessão); correm com `tests/run_lakefs_tests.sh`.
Provam: o g3_pre_merge aceita o que o run_day escreve e recusa o resto; uma tag gold-v* só nasce com manifest e G3; e
não se apaga. Medem também se o lakeFS aceita `+` no nome de uma tag (ADR-019, D7)."""
import json
import os
import sys
import time
from pathlib import Path

import lakefs
import pytest
from lakefs.exceptions import LakeFSException

from fixture_gold import SATURDAY
from gold.build import gold_path
from gold.run import run_day
from silver.lake import Lake

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "quality"))
from install_hooks import ACTIONS, GOLD_ACTIONS, install  # noqa: E402

pytestmark = [pytest.mark.lakefs, pytest.mark.skipif("LAKEFS_ENDPOINT" not in os.environ, reason="sem lakeFS")]


@pytest.fixture
def scratch(gold_lake):
    """Um repositório novo com a silver (e as provas G2) do dia sintético no main e TODOS os hooks instalados."""
    env = os.environ
    name = f"gold-hooks-test-{int(time.time() * 1000)}"
    client = lakefs.Client(host=env["LAKEFS_ENDPOINT"], username=env["LAKEFS_ACCESS_KEY_ID"], password=env["LAKEFS_SECRET_ACCESS_KEY"])
    repo = lakefs.Repository(name, client=client).create(storage_namespace=f"s3://lake/{name}", default_branch="main")
    try:
        lake = Lake(env["LAKEFS_ENDPOINT"], env["LAKEFS_ACCESS_KEY_ID"], env["LAKEFS_SECRET_ACCESS_KEY"], name)
        for path, data in gold_lake[0].branches["main"]["committed"].items():
            if path.startswith(("silver/dt=2026-10-3", "_gates/g2/dt=2026-10-3", "silver/dt=2026-11-01/hour=00", "silver/dt=2026-11-01/hour=01")):
                lake.upload("main", path, data)
        lake.commit("main", "silver sintética", {})
        assert install(repo, actions={**ACTIONS, **GOLD_ACTIONS})
        yield lake, repo
    finally:
        repo.delete()


def refused(callable_):
    with pytest.raises(LakeFSException) as error:
        callable_()
    return str(error.value)


def test_the_g3_hook_accepts_the_gold_the_pipeline_writes(scratch):
    lake, _ = scratch
    result = run_day(lake, SATURDAY)                                   # merge real para o main, com o hook ativo
    assert result.g3_status in ("pass", "warn") and lake.list("main", "gold/fact_asset_hour/dt=2026-10-31/")


def test_the_g3_hook_refuses_gold_without_proof_or_changed_after_the_proof(scratch):
    lake, repo = scratch
    path = gold_path("fact_asset_hour", SATURDAY)
    repo.branch("sem-prova").create(source_reference="main")
    repo.branch("sem-prova").object(path).upload(data="x", mode="w")
    repo.branch("sem-prova").commit("gold sem prova")
    assert "sem atestação" in refused(lambda: repo.branch("sem-prova").merge_into(repo.branch("main"), message="m"))

    run_day(lake, SATURDAY, target="main", keep_branch=True)
    lake.ensure_branch("alterada", "main")
    lake.upload("alterada", path, b"ALTERADO DEPOIS DO GATE")
    lake.commit("alterada", "adulterar", {})
    assert "checksum" in refused(lambda: repo.branch("alterada").merge_into(repo.branch("main"), message="m"))


def _release(lake, repo, tag):
    """Escreve um manifest mínimo (o que o hook confere) para o dia de sábado e junta-o ao main."""
    att = json.loads(lake.read("main", "_gates/g3/dt=2026-10-31.json"))
    manifest = {"release": tag, "tables": {p: {"checksum": c} for p, c in att["files"].items()}}
    branch = f"rel-{int(time.time() * 1000)}"
    lake.ensure_branch(branch, "main")
    lake.upload(branch, f"_releases/{tag}/manifest.json", json.dumps(manifest).encode())
    lake.commit(branch, f"release {tag}", {})
    lake.merge(branch, "main", f"release {tag}")
    return lake.resolve("main")


def test_a_release_tag_needs_the_manifest_and_g3_and_cannot_be_deleted(scratch):
    lake, repo = scratch
    run_day(lake, SATURDAY)
    commit = lake.resolve("main")
    assert "manifest" in refused(lambda: repo.tag("gold-v1.0.0_202611010240").create(commit))   # sem manifest: não
    for tag in ("gold-v1.0.0+202611010240", "gold-v1.0.0_202611010240"):        # D7: o "+" é aceite? (fica medido)
        try:
            repo.tag(tag).create(_release(lake, repo, tag))
            print(f"tag aceite: {tag}")
        except LakeFSException as error:
            print(f"tag recusada: {tag}: {error}")
    created = [t.id for t in repo.tags() if t.id.startswith("gold-v")]
    assert created, "nenhuma tag de release foi criada"
    assert "imutáveis" in refused(lambda: repo.tag(created[0]).delete())
    repo.tag("outra").create(commit)                                       # as outras tags continuam livres
    repo.tag("outra").delete()
