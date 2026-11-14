"""Os hooks Lua do G2 no lakeFS REAL (versão open source), num repositório descartável criado só para o teste.
Prova duas coisas: o hook aceita a atestação que o Python gera, e recusa tudo o resto, mesmo sem passar pelo Python."""
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import lakefs
import pytest
from lakefs.exceptions import LakeFSException

from silver.lake import Lake
from silver.run import GateFailed, run_hour

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "quality"))
from install_hooks import install  # noqa: E402

pytestmark = [pytest.mark.lakefs, pytest.mark.skipif("LAKEFS_ENDPOINT" not in os.environ, reason="sem lakeFS")]

NORMAL, GAP = datetime(2026, 11, 3, 9, tzinfo=timezone.utc), datetime(2026, 11, 2, 10, tzinfo=timezone.utc)


@pytest.fixture
def scratch(week):
    """Um repositório novo, com os hooks instalados e o bronze (hora normal e hora do comms_gap) no main."""
    env = os.environ
    name = f"gates-test-{int(time.time() * 1000)}"
    client = lakefs.Client(host=env["LAKEFS_ENDPOINT"], username=env["LAKEFS_ACCESS_KEY_ID"], password=env["LAKEFS_SECRET_ACCESS_KEY"])
    repo = lakefs.Repository(name, client=client).create(storage_namespace=f"s3://lake/{name}", default_branch="main")
    try:
        lake = Lake(env["LAKEFS_ENDPOINT"], env["LAKEFS_ACCESS_KEY_ID"], env["LAKEFS_SECRET_ACCESS_KEY"], name)
        for path, data in week[1].items():
            if ("dt=2026-11-03/hour=09/" in path) or ("dt=2026-11-02/hour=10/" in path):
                lake.upload("main", path, data)
        lake.commit("main", "bronze sintético", {})
        assert install(repo)
        yield lake, repo
    finally:
        repo.delete()


def refused(callable_):
    with pytest.raises(LakeFSException) as error:
        callable_()
    return str(error.value)


def test_the_hook_accepts_the_attestation_the_pipeline_writes(scratch):
    lake, repo = scratch
    result = run_hour(lake, NORMAL)                                   # merge real para o main, com o hook ativo
    assert result.g2_status == "pass" and lake.list("main", "silver/dt=2026-11-03/hour=09/")
    assert lake.list("main", "_gates/g2/dt=2026-11-03/")               # a prova foi junta ao main


def test_the_hook_refuses_a_blocked_hour_even_when_python_is_bypassed(scratch):
    lake, repo = scratch
    with pytest.raises(GateFailed):                                    # 1.ª linha: o próprio pipeline recusa
        run_hour(lake, GAP)
    assert not lake.list("main", "silver/")
    message = refused(lambda: repo.branch("silver-2026-11-02-10").merge_into(repo.branch("main"), message="forçado"))
    assert "G2" in message and "falhou" in message                    # 2.ª linha: o lakeFS recusa o merge forçado
    assert not lake.list("main", "silver/")


def test_the_hook_refuses_silver_without_proof_or_changed_after_the_proof(scratch):
    lake, repo = scratch
    path = "silver/dt=2026-11-03/hour=09/data.parquet"
    repo.branch("sem-prova").create(source_reference="main")
    repo.branch("sem-prova").object(path).upload(data="x", mode="w")
    repo.branch("sem-prova").commit("silver sem prova")
    assert "sem atestação" in refused(lambda: repo.branch("sem-prova").merge_into(repo.branch("main"), message="m"))

    lake.ensure_branch("alterada", "main")
    run_hour(lake, NORMAL, target="alterada", keep_branch=True)         # gera silver + atestação válidas…
    lake.upload("alterada", path, b"ALTERADO DEPOIS DO GATE")           # …e mexe no ficheiro depois
    lake.commit("alterada", "adulterar", {})
    assert "checksum" in refused(lambda: repo.branch("alterada").merge_into(repo.branch("main"), message="m"))


def test_nobody_can_delete_the_hook_with_a_normal_commit(scratch):
    lake, repo = scratch
    repo.branch("fuga").create(source_reference="main")
    repo.branch("fuga").object("_lakefs_actions/g2.yaml").delete()
    assert "_lakefs_actions" in refused(lambda: repo.branch("fuga").commit("apagar o hook"))


def test_a_protected_main_refuses_direct_commits_but_the_installer_still_works(scratch):
    lake, repo = scratch
    # proteção de branch no main (a mesma que scripts/lakefs-repo.sh do repositório platform aplica)
    from lakefs_sdk import BranchProtectionRule
    repo._BaseLakeFSObject__client.sdk_client.repositories_api.set_branch_protection_rules(repo.id, [BranchProtectionRule(pattern="main")])
    # o lakeFS guarda as regras do repositório em cache: num repositório já usado, entram em vigor ao fim de algum tempo
    deadline = time.time() + 120
    while True:
        try:
            repo.branch("main").object("x.txt").upload(data=b"x")
        except LakeFSException as error:
            assert "protected branch" in str(error)
            break
        repo.branch("main").reset_changes(path_type="reset")
        assert time.time() < deadline, "a proteção do main não entrou em vigor"
        time.sleep(5)
    assert install(repo) is False                                      # já estava instalado: nada a fazer, mesmo protegido
