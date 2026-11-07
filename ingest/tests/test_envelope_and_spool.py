import yaml
from pathlib import Path

from helpers import NOW, SEC, sample
from ingest.bronze import load_nodes
from ingest.envelope import Event, from_us, make_batch_id, make_event_id, to_us
from ingest.spool import Spool

GATEWAY = Path(__file__).resolve().parents[2] / "ot" / "gateway" / "src" / "ot_gateway" / "devices.yaml"


def test_event_id_is_deterministic_and_ignores_receive_times():
    a = Event.build("ot-gateway", "ns=2;s=X", "5.07", 0, NOW, NOW + 5, NOW + 10)
    b = Event.build("ot-gateway", "ns=2;s=X", "5.07", 0, NOW, NOW + 999, NOW + 999)   # reentrega depois de reconectar
    c = Event.build("ot-gateway", "ns=2;s=X", "5.08", 0, NOW, NOW + 5, NOW + 10)
    assert a.event_id == b.event_id != c.event_id
    assert a.event_id == make_event_id("ot-gateway", "ns=2;s=X", NOW, 0, "5.07")


def test_microsecond_roundtrip_is_exact():
    moment = from_us(1794063600123456)
    assert to_us(moment) == 1794063600123456 and moment.microsecond == 123456


def test_batch_id_ignores_order():
    assert make_batch_id(["b", "a", "c"]) == make_batch_id(["c", "a", "b"])


def test_nodes_yaml_matches_the_gateway_config():
    kinds = {"analog": "double", "counter": "uint32", "state": "uint16", "clock": "datetime"}
    expected = {}
    for device in yaml.safe_load(GATEWAY.read_text())["devices"]:
        expected[f"ns=2;s={device['name']}.Connected"] = "boolean"
        for signal in device["signals"]:
            expected[f"ns=2;s={device['name']}.{signal['name']}"] = kinds[signal.get("kind", "analog")]
        if device.get("alarm_coil") is not None:
            expected[f"ns=2;s={device['name']}.alarm"] = "boolean"
    assert load_nodes() == expected


def test_spool_ignores_repeated_samples(dsn):
    spool = Spool(dsn)
    events = [sample(i) for i in range(5)]
    assert spool.add(events) == 5
    assert spool.add(events + [sample(99)]) == 1            # as 5 repetidas são ignoradas
    assert spool.counts()["events"] == 6


def test_batch_forms_when_full_or_when_the_oldest_waits_too_long(dsn):
    spool = Spool(dsn)
    spool.add([sample(i) for i in range(3)])
    just_arrived = NOW - 3600 * SEC + 10 * SEC                                          # a mais antiga espera há 10 s
    assert spool.assign_batch(max_rows=10, max_age_s=60, now_us=just_arrived) is None   # nem cheio nem velho
    assert spool.assign_batch(max_rows=10, max_age_s=60, now_us=NOW + 7200 * SEC) is not None   # esperou demais
    spool.add([sample(i) for i in range(10, 20)])
    batch = spool.assign_batch(max_rows=10, max_age_s=3600, now_us=NOW)
    assert batch is not None and batch.rows == 10                                         # encheu


def test_a_frozen_batch_never_regroups(dsn):
    spool = Spool(dsn)
    spool.add([sample(i) for i in range(4)])
    batch = spool.assign_batch(10, 0, NOW + 7200 * SEC)
    before = [e.event_id for e in spool.rows(batch.batch_id)]
    spool.add([sample(i) for i in range(100, 105)])            # chegam amostras novas depois
    assert [e.event_id for e in spool.rows(batch.batch_id)] == before
    assert make_batch_id(before) == batch.batch_id


def test_spool_survives_a_restart_and_two_processes_can_share_it(dsn):
    collector, writer = Spool(dsn), Spool(dsn)                 # dois processos, a mesma base de dados
    collector.add([sample(i) for i in range(6)])
    batch = writer.assign_batch(6, 0, NOW)
    collector.add([sample(50)])                                # o collector continua a escrever
    collector.close()
    writer.close()
    reopened = Spool(dsn)                                      # "reinício"
    assert [b.batch_id for b in reopened.unfinished()] == [batch.batch_id]
    assert reopened.counts()["events"] == 7 and reopened.counts()["unassigned"] == 1
