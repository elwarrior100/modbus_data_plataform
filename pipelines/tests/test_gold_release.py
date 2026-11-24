"""Releases do gold contra um lakeFS em memória: tag + manifest, o LATEST_STABLE, o rollback e o rastreio até ao bronze
(DoD da Fase 6). O comportamento dos hooks no lakeFS real prova-se em test_gates_hook_gold.py."""
import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from fixture_gold import SATURDAY, SUNDAY
from gold import release as rel
from gold.run import run_day

UTC = timezone.utc
T1, T2, T3 = (datetime(2026, 11, d, 2, 40, tzinfo=UTC) for d in (2, 3, 4))


def _tag_hook(lake):
    """O mesmo que o hook release_pre_create_tag confere: manifest na tag, G3 sem fail, checksums certos."""
    def check(name, commit):
        m = json.loads(lake.read(commit, rel.manifest_path(name)))
        assert m["release"] == name
        att = json.loads(lake.read(commit, f"_gates/g3/dt={m['covers']['day']}.json"))
        assert att["status"] != "fail"
        assert {p: t["checksum"] for p, t in m["tables"].items()} == att["files"]
    return check


@pytest.fixture(scope="module")
def two_days(gold_lake):
    lake = copy.deepcopy(gold_lake[0])
    run_day(lake, SATURDAY)
    run_day(lake, SUNDAY)
    return lake


@pytest.fixture
def lake(two_days):
    lake = copy.deepcopy(two_days)
    lake.tag_hook = _tag_hook(lake)
    return lake


def _reprocess(lake, day):
    """Simula um reprocessamento do dia: um ficheiro gold muda e a atestação G3 acompanha (como um run_day novo)."""
    committed = lake.branches["main"]["committed"]
    path = f"gold/dim_asset/dt={day}/data.parquet"
    gate = f"_gates/g3/dt={day}.json"
    import io
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pq.read_table(io.BytesIO(committed[path]))
    buf = io.BytesIO()
    pq.write_table(table.append_column("reprocessed", pa.array([True] * table.num_rows)), buf)
    committed[path] = buf.getvalue()
    att = json.loads(committed[gate])
    att["files"][path] = hashlib.md5(committed[path]).hexdigest()
    committed[gate] = json.dumps(att).encode()


def test_three_releases_the_pointer_follows_and_a_reprocess_is_a_patch(lake):
    """DoD: ≥3 releases. Sábado e domingo são 1.0.0; reprocessar domingo dá 1.0.1. O ponteiro segue a mais recente."""
    sat = rel.release_day(lake, SATURDAY, now=T1)
    sun = rel.release_day(lake, SUNDAY, now=T2)
    _reprocess(lake, SUNDAY)
    patch = rel.release_day(lake, SUNDAY, now=T3)
    assert [sat.tag, sun.tag, patch.tag] == ["gold-v1.0.0_202611020240", "gold-v1.0.0_202611030240", "gold-v1.0.1_202611040240"]
    assert all(r.created and r.pointer_moved and r.g3 == "pass" for r in (sat, sun, patch))
    assert [t for t, _ in rel.releases(lake)] == [sat.tag, sun.tag, patch.tag]
    assert rel.current(lake) == patch.tag
    # cada tag aponta para um commit que tem o seu manifest; e esse commit não muda (a tag é imutável)
    for r in (sat, sun, patch):
        assert rel.read_manifest(lake, r.tag)["release"] == r.tag and dict(lake.tags())[r.tag] == r.commit


def test_the_manifest_has_what_the_plan_asks(lake):
    r = rel.release_day(lake, SUNDAY, now=T2)
    m = rel.read_manifest(lake, r.tag)
    assert m["covers"] == {"day": SUNDAY.isoformat(), "from": f"{SUNDAY}T00:00:00Z", "to": "2026-11-02T00:00:00Z"}
    assert set(m["tables"]) == {p for p in lake.list("main", "gold/") if f"dt={SUNDAY}" in p}
    assert all(t["rows"] > 0 and t["checksum"] for t in m["tables"].values())
    assert m["gates"]["g3"] == "pass" and m["gates"]["g2"] and m["code"]["dag"] == "gold_daily"
    assert m["inputs"]["silver_commit"] and m["schema_version"] == "1.0.0"


def test_releasing_the_same_gold_again_creates_nothing(lake):
    first = rel.release_day(lake, SUNDAY, now=T2)
    again = rel.release_day(lake, SUNDAY, now=T3)
    assert not again.created and again.tag == first.tag and len(rel.releases(lake)) == 1


def test_rollback_points_back_without_copying_data_and_is_fast(lake):
    sat = rel.release_day(lake, SATURDAY, now=T1)
    sun = rel.release_day(lake, SUNDAY, now=T2)
    gold_before = {p: lake.checksum("main", p) for p in lake.list("main", "gold/")}
    before, after, seconds = rel.rollback(lake)
    assert (before, after) == (sun.tag, sat.tag) and rel.current(lake) == sat.tag
    assert seconds < 60                                            # DoD: < 1 min (aqui, em memória, milissegundos)
    assert {p: lake.checksum("main", p) for p in lake.list("main", "gold/")} == gold_before    # nenhum dado mexeu
    rel.rollback(lake, to=sun.tag)                                 # e volta-se para a frente da mesma forma
    assert rel.current(lake) == sun.tag
    with pytest.raises(rel.ReleaseError):
        rel.point(lake, "gold-v9.9.9_209901010000")                # uma release que não existe


def test_repeating_an_old_day_never_moves_the_pointer_back(lake):
    rel.release_day(lake, SATURDAY, now=T1)
    sun = rel.release_day(lake, SUNDAY, now=T2)
    _reprocess(lake, SATURDAY)
    patch = rel.release_day(lake, SATURDAY, now=T3)               # um PATCH de sábado, depois do domingo
    assert patch.created and not patch.pointer_moved and rel.current(lake) == sun.tag


def test_a_warning_creates_the_release_but_does_not_move_the_pointer(lake):
    rel.release_day(lake, SATURDAY, now=T1)
    gate = f"_gates/g3/dt={SUNDAY}.json"
    att = json.loads(lake.branches["main"]["committed"][gate])
    att["status"] = "warn"
    lake.branches["main"]["committed"][gate] = json.dumps(att).encode()
    sun = rel.release_day(lake, SUNDAY, now=T2)
    assert sun.created and sun.g3 == "warn" and not sun.pointer_moved
    rel.point(lake, sun.tag, reason="aviso aceite")                # quem decide pode avançá-lo à mão
    assert rel.current(lake) == sun.tag


def test_a_failed_g3_has_no_release(lake):
    gate = f"_gates/g3/dt={SUNDAY}.json"
    att = json.loads(lake.branches["main"]["committed"][gate])
    att["status"] = "fail"
    lake.branches["main"]["committed"][gate] = json.dumps(att).encode()
    with pytest.raises(rel.ReleaseError, match="G3 falhou"):
        rel.release_day(lake, SUNDAY, now=T2)
    assert rel.releases(lake) == []


def test_from_a_release_to_the_bronze_commits(lake):
    """DoD: a partir do manifest reconstrói-se que commits de bronze originaram a release."""
    r = rel.release_day(lake, SUNDAY, now=T2)
    t = rel.trace(lake, r.tag)
    assert t["day"] == SUNDAY.isoformat() and t["silver_commit"]
    assert t["bronze_commits"] and all(c and not c.startswith("desconhecido") for c in t["bronze_commits"])
    for path, bronze in t["hours"].items():                        # cada hora: o commit de bronze que a silver leu
        g2 = json.loads(lake.read("main", path.replace("silver/", "_gates/g2/").replace("/data.parquet", ".json")))
        assert g2["bronze_commit"] == bronze


def test_a_gold_v_tag_that_is_not_a_release_is_ignored(lake):
    """A `gold-v0.1.0` do spike da Fase 0 (no cluster local, sem manifest) não pode fazer rebentar as releases,
    a retenção da silver nem o serving, que leem o manifest de cada release."""
    from gold.retention import released_days
    lake.tag_refs["gold-v0.1.0"] = lake.resolve("main")          # como ficou do spike: sem manifest, sem carimbo
    sat = rel.release_day(lake, SATURDAY, now=T1)
    assert [t for t, _ in rel.releases(lake)] == [sat.tag]
    assert released_days(lake) == {SATURDAY.isoformat()}
    with pytest.raises(rel.ReleaseError):
        rel.point(lake, "gold-v0.1.0")                             # nem se pode apontar para ela
