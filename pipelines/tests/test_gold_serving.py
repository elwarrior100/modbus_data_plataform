"""O gold no PostgreSQL do BI (ADR-022, S1): por release, vistas que seguem o LATEST_STABLE, rollback instantâneo e
`sync` que corrige divergências. Precisa de um Postgres (SERVING_TEST_DSN); cada teste usa esquemas próprios."""
import copy
import io
import os
import time
import uuid

import psycopg
import pyarrow.parquet as pq
import pytest

from fixture_gold import SATURDAY, SUNDAY
from gold import release as rel
from gold.build import TABLES
from gold.run import run_day
from gold.serving import Serving, _stamp
from test_gold_release import T1, T2, T3, _reprocess, _tag_hook

DSN = os.environ.get("SERVING_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="sem Postgres de teste (SERVING_TEST_DSN)")


@pytest.fixture(scope="module")
def two_days(gold_lake):
    lake = copy.deepcopy(gold_lake[0])
    run_day(lake, SATURDAY)
    run_day(lake, SUNDAY)
    return lake


@pytest.fixture
def lake(two_days, monkeypatch):
    monkeypatch.delenv("SERVING_DATABASE_URL", raising=False)      # o point() não publica (os esquemas são de teste)
    monkeypatch.delenv("OPENMETADATA_URL", raising=False)
    lake = copy.deepcopy(two_days)
    lake.tag_hook = _tag_hook(lake)
    return lake


@pytest.fixture
def pg():
    prefix = "t" + uuid.uuid4().hex[:10]
    s = Serving(DSN, serving=f"{prefix}_serving", views=f"{prefix}_gold")
    yield s
    with psycopg.connect(DSN, autocommit=True) as db:
        db.execute(f"DROP SCHEMA IF EXISTS {s.v} CASCADE; DROP SCHEMA IF EXISTS {s.s} CASCADE")


def q(s, sql):
    with psycopg.connect(DSN) as db:
        return db.execute(sql.format(s=s.s, v=s.v)).fetchall()


def three_releases(lake):
    sat = rel.release_day(lake, SATURDAY, now=T1)
    sun = rel.release_day(lake, SUNDAY, now=T2)
    _reprocess(lake, SUNDAY)
    patch = rel.release_day(lake, SUNDAY, now=T3)
    return sat, sun, patch


def test_sync_copies_every_release_and_the_views_show_what_the_pointer_says(lake, pg):
    sat, sun, patch = three_releases(lake)
    r = pg.sync(lake)
    assert r.loaded == [sat.tag, sun.tag, patch.tag] and r.pointer == patch.tag and r.pointer_changed
    # cada release tem as suas linhas, iguais ao parquet da tag
    for t in TABLES:
        path = f"gold/{t}/dt={SUNDAY}/data.parquet"
        expected = pq.read_table(io.BytesIO(lake.read(sun.tag, path))).num_rows
        assert q(pg, f"SELECT count(*) FROM {{s}}.gold_{t} WHERE release = '{sun.tag}'")[0][0] == expected
    # a vista: sábado da 1.ª release, domingo do PATCH (a mais recente que não passa o ponteiro)
    assert dict(q(pg, "SELECT day::text, tag FROM {s}.visible_releases")) == {SATURDAY.isoformat(): sat.tag,
                                                                              SUNDAY.isoformat(): patch.tag}
    assert q(pg, "SELECT count(DISTINCT release) FROM {v}.dim_asset")[0][0] == 2
    assert q(pg, "SELECT bool_and(reprocessed) FROM {v}.dim_asset WHERE release = "
             f"'{patch.tag}'")[0][0] is True                 # a coluna nova do reprocessamento entrou na tabela


def test_a_rollback_changes_the_views_at_once_without_copying(lake, pg):
    sat, sun, patch = three_releases(lake)
    pg.sync(lake)
    rows_before = q(pg, "SELECT count(*) FROM {s}.gold_fact_tag_hour")[0][0]
    rel.rollback(lake)                                        # lakeFS: patch → sun
    started = time.monotonic()
    seconds = pg.set_pointer(rel.current(lake))               # o que gold.release.point faz no Postgres
    assert seconds < 1 and time.monotonic() - started < 60   # DoD: rollback < 1 min, também aqui
    assert dict(q(pg, "SELECT day::text, tag FROM {s}.visible_releases"))[SUNDAY.isoformat()] == sun.tag
    assert q(pg, "SELECT count(*) FROM {s}.gold_fact_tag_hour")[0][0] == rows_before    # nada copiado nem apagado
    rel.rollback(lake)                                        # sun → sat: o domingo deixa de se ver
    pg.set_pointer(rel.current(lake))
    assert set(dict(q(pg, "SELECT day::text, tag FROM {s}.visible_releases"))) == {SATURDAY.isoformat()}


def test_check_finds_a_divergence_and_sync_fixes_it(lake, pg):
    sat, sun, patch = three_releases(lake)
    pg.sync(lake)
    assert pg.check(lake) == []
    rel.rollback(lake)                                        # o lakeFS mudou, o Postgres não soube (falha de rede)
    assert any("ponteiro" in p for p in pg.check(lake))
    r = pg.sync(lake)
    assert r.loaded == [] and r.pointer_changed and pg.check(lake) == []
    assert pg.sync(lake).pointer_changed is False             # idempotente


def test_order_is_by_the_tag_stamp_never_by_the_version_text(pg):
    """Em texto, 'gold-v1.10.0' < 'gold-v1.9.0'. A vista tem de escolher a mais recente pelo carimbo."""
    older, newer = "gold-v1.9.0_202611040240", "gold-v1.10.0_202611050240"
    assert _stamp(newer) > _stamp(older) and newer < older
    with psycopg.connect(DSN, autocommit=True) as db:
        for tag in (older, newer):
            db.execute(f"INSERT INTO {pg.s}.releases (tag, stamp, day, g3, lakefs_commit) VALUES "
                       "(%s, %s, '2026-11-03', 'pass', 'c')", (tag, _stamp(tag)))
    pg.set_pointer(newer)
    assert q(pg, "SELECT tag FROM {s}.visible_releases") == [(newer,)]
    pg.set_pointer(older)                                     # rollback para a anterior: a mais nova deixa de se ver
    assert q(pg, "SELECT tag FROM {s}.visible_releases") == [(older,)]


def test_reloading_a_release_replaces_it(lake, pg):
    sat = rel.release_day(lake, SATURDAY, now=T1)
    pg.sync(lake)
    n = q(pg, "SELECT count(*) FROM {s}.gold_fact_tag_minute")[0][0]
    pg.load_release(lake, sat.tag, dict(lake.tags())[sat.tag])
    assert q(pg, "SELECT count(*) FROM {s}.gold_fact_tag_minute")[0][0] == n > 0
