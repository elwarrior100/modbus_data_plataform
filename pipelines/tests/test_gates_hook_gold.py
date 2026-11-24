"""Os hooks do gold (G3 e tags de release) no lakeFS REAL, num repositório descartável criado só para o teste.
NÃO corridos ainda (2026-11-23: sem lakeFS à mão nesta sessão); correm com `tests/run_lakefs_tests.sh`.
Provam: o g3_pre_merge aceita o que o run_day escreve e recusa o resto; uma tag gold-v* só nasce com manifest e G3; e
não se apaga. Medem também se o lakeFS aceita `+` no nome de uma tag (ADR-019, D7)."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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


def test_releases_pointer_and_rollback_on_the_real_lakefs(scratch):
    """O ciclo inteiro no lakeFS real, com TODOS os hooks: release (tag + manifest), o LATEST_STABLE a seguir, um
    PATCH depois de um reprocessamento, o rollback cronometrado (DoD: < 1 min), e o hook a recusar um ponteiro falso."""
    import hashlib
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    from gold import release as rel
    from gold.run import run_day

    lake, repo = scratch
    run_day(lake, SATURDAY)
    first = rel.release_day(lake, SATURDAY)
    assert first.created and first.pointer_moved and rel.current(lake) == first.tag
    print(f"release: {first.tag}")

    # um reprocessamento de sábado (um ficheiro gold muda, com a prova G3 a acompanhar), pelo merge com hooks
    path, gate = f"gold/dim_asset/dt={SATURDAY}/data.parquet", f"_gates/g3/dt={SATURDAY}.json"
    table = pq.read_table(io.BytesIO(lake.read("main", path)))
    buf = io.BytesIO()
    pq.write_table(table.append_column("reprocessed", pa.array([True] * table.num_rows)), buf)
    lake.ensure_branch("reprocess", "main")
    lake.upload("reprocess", path, buf.getvalue())
    att = json.loads(lake.read("main", gate))
    att["files"][path] = lake.checksum("reprocess", path)
    lake.upload("reprocess", gate, json.dumps(att).encode())
    lake.commit("reprocess", "gold: reprocessar sábado", {})
    lake.merge("reprocess", "main", "gold: reprocessar sábado")
    second = rel.release_day(lake, SATURDAY, now=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert second.created and second.tag.startswith("gold-v1.0.1_") and rel.current(lake) == second.tag

    before, after, seconds = rel.rollback(lake)
    print(f"rollback {before} -> {after} em {seconds:.2f} s")
    assert after == first.tag and rel.current(lake) == first.tag and seconds < 60

    # ADR-022 (S1): as duas releases copiadas do lakeFS REAL (lidas pelas tags) para o Postgres do BI, e a vista
    # a mostrar a que o LATEST_STABLE diz (a primeira, depois do rollback). Só com um Postgres (SERVING_TEST_DSN).
    if os.environ.get("SERVING_TEST_DSN"):
        import uuid

        import psycopg

        from gold.serving import Serving
        prefix = "t" + uuid.uuid4().hex[:10]
        s = Serving(os.environ["SERVING_TEST_DSN"], serving=f"{prefix}_serving", views=f"{prefix}_gold")
        try:
            r = s.sync(lake)
            assert r.loaded == [first.tag, second.tag] and r.pointer == first.tag and s.check(lake) == []
            with psycopg.connect(os.environ["SERVING_TEST_DSN"]) as db:
                seen = db.execute(f"SELECT DISTINCT release FROM {s.v}.kpi_asset_day").fetchall()
                rows = db.execute(f"SELECT count(*) FROM {s.v}.fact_tag_hour").fetchone()[0]
            assert seen == [(first.tag,)] and rows > 0
            print(f"serving: {r.rows} linhas de {len(r.loaded)} releases; a vista mostra {first.tag} ({rows} horas)")
        finally:
            with psycopg.connect(os.environ["SERVING_TEST_DSN"], autocommit=True) as db:
                db.execute(f"DROP SCHEMA IF EXISTS {s.v} CASCADE; DROP SCHEMA IF EXISTS {s.s} CASCADE")

    # o hook do ponteiro recusa uma release que não existe (mesmo escrevendo o ficheiro à mão)
    lake.ensure_branch("bad-pointer", "main")
    lake.upload("bad-pointer", rel.POINTER, json.dumps({"release": "gold-v9.9.9_209901010000"}).encode())
    lake.commit("bad-pointer", "ponteiro falso", {})
    assert "não existe" in refused(lambda: lake.merge("bad-pointer", "main", "ponteiro falso"))
    assert rel.current(lake) == first.tag

    # do manifest ao bronze, e o gold lido PELA TAG (com "+") na porta S3 do lakeFS, como os consumidores da Fase 7
    assert rel.trace(lake, first.tag)["silver_commit"]
    import duckdb
    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs")
    except duckdb.Error as error:                                  # sem internet para a extensão: fica registado
        pytest.skip(f"sem a extensão httpfs do DuckDB: {error}")
    env = os.environ
    endpoint = env["LAKEFS_ENDPOINT"].split("://", 1)[-1]
    con.execute(f"""CREATE SECRET lake (TYPE s3, KEY_ID '{env["LAKEFS_ACCESS_KEY_ID"]}', SECRET '{env["LAKEFS_SECRET_ACCESS_KEY"]}',
                    ENDPOINT '{endpoint}', URL_STYLE 'path', USE_SSL false, REGION 'us-east-1')""")
    rows = con.execute(f"SELECT count(*) FROM read_parquet('s3://{repo.id}/{first.tag}/gold/kpi_asset_day/dt={SATURDAY}/data.parquet')").fetchone()[0]
    print(f"lido pela tag {first.tag} na porta S3: {rows} linhas")
    assert rows == rel.read_manifest(lake, first.tag)["tables"][f"gold/kpi_asset_day/dt={SATURDAY}/data.parquet"]["rows"]
