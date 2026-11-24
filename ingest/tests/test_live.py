"""Caminho live (ADR-022, L1): o trigger no spool copia cada amostra nova para a hypertable, e NUNCA faz falhar a
escrita do collector. Os testes com base precisam de um Postgres com a extensão timescaledb (INGEST_TEST_DSN)."""
import psycopg
import pytest

from helpers import NOW, SEC, sample
from ingest.bronze import load_nodes
import threading
import time

from ingest.live import ensure, install, installed, keep_installed, node_rows, try_install
from ingest.spool import Spool


def test_the_node_table_is_the_nodes_yaml_without_the_clocks():
    nodes = load_nodes()
    rows = {r[0]: r for r in node_rows()}
    assert set(rows) == {n for n, k in nodes.items() if k != "datetime"}
    assert rows["ns=2;s=PUMP-01.flow_rate"] == ("ns=2;s=PUMP-01.flow_rate", "PUMP-01", "flow_rate", "number")
    assert rows["ns=2;s=MOTOR-01.alarm"][3] == "boolean"
    assert rows["ns=2;s=PUMP-01.run_hours"][3] == "number"


@pytest.fixture
def live(dsn):
    """Um spool num esquema novo, com o live num esquema próprio ao lado (apagado no fim)."""
    with psycopg.connect(dsn, autocommit=True) as db:
        if db.execute("SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'").fetchone()[0] == 0:
            pytest.skip("a base de teste não tem a extensão timescaledb")
        schema = db.execute("SELECT current_schema()").fetchone()[0]
    name = schema + "_live"
    spool = Spool(dsn)
    install(dsn, live=name)
    yield spool, dsn, name
    spool.close()
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def rows(dsn, live, where="true"):
    with psycopg.connect(dsn) as db:
        return db.execute(f"SELECT asset, tag, value, status, extract(epoch from time)::numeric, clock_skew "
                          f"FROM {live}.readings WHERE {where} ORDER BY time, tag").fetchall()


def failures(dsn, live):
    with psycopg.connect(dsn) as db:
        return db.execute(f"SELECT failures, last_error FROM {live}.health").fetchone()


def test_a_new_sample_is_in_the_hypertable_at_once_and_a_repeat_is_not_copied_twice(live):
    spool, dsn, name = live
    e = sample(1)
    assert spool.add([e, e]) == 1
    assert spool.add([e]) == 0                        # reentrega (ON CONFLICT DO NOTHING): o trigger não corre
    got = rows(dsn, name)
    assert len(got) == 1
    asset, tag, value, status, t, skew = got[0]
    assert (asset, tag, value, status, skew) == ("PUMP-01", "outlet_pressure", 5.001, 0, False)
    assert t * SEC == e.source_ts                     # o instante do dispositivo, exato ao microssegundo


def test_booleans_bad_readings_clocks_and_unknown_nodes(live):
    spool, dsn, name = live
    spool.add([sample(1, node="ns=2;s=MOTOR-01.alarm", value="true"),
               sample(2, node="ns=2;s=MOTOR-01.Connected", value="false"),
               sample(3, node="ns=2;s=PUMP-01.flow_rate", status=0x80000000),        # leitura má: valor NULL
               sample(4, node="ns=2;s=PUMP-01.device_clock", value='"2026-11-07T14:30:04+00:00"'),
               sample(5, node="ns=2;s=INVENTADO.sensor", value="1.0")])
    got = {(r[1], r[2], r[3]) for r in rows(dsn, name)}
    assert got == {("alarm", 1.0, 0), ("Connected", 0.0, 0), ("flow_rate", None, 0x80000000)}
    assert failures(dsn, name)[0] == 0


def test_a_device_clock_off_by_more_than_a_minute_uses_the_gateway_time(live):
    spool, dsn, name = live
    e = sample(1, source_ts=NOW - 10 * 60 * SEC)       # dispositivo 10 min atrasado em relação ao gateway
    spool.add([e])
    (_, _, _, _, t, skew), = rows(dsn, name)
    assert skew is True and t * SEC == e.server_ts


def test_a_value_that_does_not_convert_never_fails_the_spool(live):
    spool, dsn, name = live
    bad = sample(1, value='"não é um número"')
    good = sample(2)
    assert spool.add([bad, good]) == 2                # as duas ficam no spool (o bronze recebe as duas)
    assert spool.counts()["events"] == 2
    assert [r[2] for r in rows(dsn, name)] == [5.002]  # só a boa chega ao live
    n, error = failures(dsn, name)
    assert n == 1 and "double precision" in error


def test_without_the_live_schema_the_spool_keeps_working(live):
    """Como depois de restaurar um dump (o esquema live fica de fora): o trigger está lá, a hypertable não."""
    spool, dsn, name = live
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(f"DROP SCHEMA {name} CASCADE")
    assert spool.add([sample(1), sample(2)]) == 2
    assert spool.counts()["events"] == 2


def test_install_is_idempotent_and_follows_the_nodes_yaml(live):
    spool, dsn, name = live
    install(dsn, live=name)
    install(dsn, live=name, nodes={"ns=2;s=PUMP-01.flow_rate": "double"})   # um nodes.yaml mais pequeno
    with psycopg.connect(dsn) as db:
        assert db.execute(f"SELECT node_id FROM {name}.nodes").fetchall() == [("ns=2;s=PUMP-01.flow_rate",)]
    spool.add([sample(1), sample(2, node="ns=2;s=PUMP-01.flow_rate")])
    assert [r[1] for r in rows(dsn, name)] == ["flow_rate"]


def test_try_install_without_timescale_does_not_raise():
    assert try_install("postgresql://ninguem@127.0.0.1:1/nada?connect_timeout=1") is False


def test_the_keeper_puts_back_a_live_schema_dropped_while_the_collector_runs(live):
    """Um restauro (ou um DROP à mão) apaga o live com o collector a correr: na volta seguinte o vigia repõe-no e
    as amostras novas voltam a entrar, sem reiniciar o collector."""
    spool, dsn, name = live
    assert installed(dsn, live=name) and ensure(dsn, live=name)          # inteiro: o vigia não mexe em nada
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(f"DROP SCHEMA {name} CASCADE")
    assert not installed(dsn, live=name)
    spool.add([sample(1)])                                                 # perdida para o live, não para o spool
    assert ensure(dsn, live=name) and installed(dsn, live=name)
    spool.add([sample(2)])
    assert len(rows(dsn, name)) == 1 and spool.counts()["events"] == 2


def test_the_keeper_notices_a_disabled_trigger(live):
    spool, dsn, name = live
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("ALTER TABLE events DISABLE TRIGGER live_copy")
    assert not installed(dsn, live=name)
    assert ensure(dsn, live=name) and installed(dsn, live=name)           # o CREATE OR REPLACE volta a ativá-lo


def test_the_keeper_installs_when_the_extension_arrives_later(new_dsn, monkeypatch):
    """O caso da promoção de 23-11: o collector arranca, o live ainda não se instala (aqui finge-se a extensão em
    falta); uma volta depois instala-se sozinho, sem reinício."""
    from ingest import live as mod
    dsn = new_dsn()
    with psycopg.connect(dsn, autocommit=True) as db:
        if db.execute("SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'").fetchone()[0] == 0:
            pytest.skip("a base de teste não tem a extensão timescaledb")
        schema = db.execute("SELECT current_schema()").fetchone()[0]
    name = schema + "_live"
    spool = Spool(dsn)
    real, calls = mod.install, []

    def install_later(*a, **k):                     # 1.ª tentativa: "a extensão timescaledb não está criada"
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.errors.UndefinedObject("a extensão timescaledb não está criada nesta base")
        return real(*a, **k)

    monkeypatch.setattr(mod, "install", install_later)
    stop = threading.Event()
    t = threading.Thread(target=keep_installed, args=(dsn, stop), kwargs={"every": 0.2, "live": name}, daemon=True)
    try:
        t.start()
        deadline = time.monotonic() + 10
        while not installed(dsn, live=name) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert installed(dsn, live=name) and len(calls) == 2
        time.sleep(0.6)                              # as voltas seguintes só verificam: não reinstalam
        assert len(calls) == 2
        spool.add([sample(1)])
        assert len(rows(dsn, name)) == 1
    finally:
        stop.set()
        t.join(5)
        spool.close()
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    assert not t.is_alive()


def test_ensure_without_a_database_does_not_raise():
    assert ensure("postgresql://ninguem@127.0.0.1:1/nada?connect_timeout=1") is False
