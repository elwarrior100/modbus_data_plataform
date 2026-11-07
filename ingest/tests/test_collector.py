"""O collector contra um servidor OPC UA de teste que imita o gateway."""
import asyncio
import socket
from datetime import datetime, timezone

import pytest
from asyncua import Server, ua

from ingest.collector import NAMESPACE_URI, Collector
from ingest.envelope import from_us
from ingest.spool import Spool

T0 = datetime(2026, 11, 7, 15, 0, 0, tzinfo=timezone.utc)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeGateway:
    """Um servidor OPC UA mínimo, com a mesma árvore e os mesmos NodeIds do gateway real."""

    def __init__(self, port):
        self.port, self.server, self.nodes = port, None, {}

    async def start(self):
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(f"opc.tcp://127.0.0.1:{self.port}/ot-gateway")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await self.server.register_namespace(NAMESPACE_URI)
        plant = await self.server.nodes.objects.add_object(ua.NodeId("Plant", self.ns), ua.QualifiedName("Plant", self.ns))
        asset = await plant.add_object(ua.NodeId("PUMP-01", self.ns), ua.QualifiedName("PUMP-01", self.ns))
        for name, vtype in (("outlet_pressure", ua.VariantType.Double), ("state", ua.VariantType.UInt16)):
            nid = ua.NodeId(f"PUMP-01.{name}", self.ns)
            self.nodes[name] = nid
            await asset.add_variable(nid, ua.QualifiedName(name, self.ns), ua.Variant(0 if vtype is ua.VariantType.UInt16 else 0.0, vtype))
        await self.server.start()

    async def write(self, name, value, vtype, seconds=0, status=ua.StatusCodes.Good):
        moment = T0.replace(second=seconds)
        data = ua.DataValue(None if value is None else ua.Variant(value, vtype), ua.StatusCode(status), moment, moment)
        await self.server.write_attribute_value(self.nodes[name], data)

    async def stop(self):
        await self.server.stop()


async def until(predicate, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condição não cumprida a tempo"
        await asyncio.sleep(0.05)


@pytest.fixture
async def rig(dsn):
    gateway = FakeGateway(free_port())
    await gateway.start()
    spool = Spool(dsn)
    collector = Collector(f"opc.tcp://127.0.0.1:{gateway.port}/ot-gateway", spool, period_ms=50, heartbeat_s=0.3, backoff_max_s=1)
    task = asyncio.create_task(collector.run())
    yield gateway, collector, spool, task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    try:
        await gateway.stop()
    except Exception:
        pass


def rows(spool):
    return spool.db.execute("SELECT opcua_node_id, raw_value, opcua_status_code, source_ts FROM events ORDER BY seq").fetchall()


async def test_collects_values_with_status_and_timestamps(rig):
    gateway, collector, spool, _ = rig
    await collector.connected.wait()
    await gateway.write("outlet_pressure", 5.07, ua.VariantType.Double, seconds=1)
    await gateway.write("state", 2, ua.VariantType.UInt16, seconds=1)
    await until(lambda: any(r[1] == "5.07" for r in rows(spool)) and any(r[1] == "2" for r in rows(spool)))
    pressure = next(r for r in rows(spool) if r[1] == "5.07")
    assert pressure[0] == "ns=2;s=PUMP-01.outlet_pressure" and pressure[2] == 0
    assert from_us(pressure[3]) == T0.replace(second=1)                      # o relógio do dispositivo, não o nosso


async def test_a_bad_value_is_recorded_with_its_status_and_no_value(rig):
    gateway, collector, spool, _ = rig
    await collector.connected.wait()
    await gateway.write("outlet_pressure", 5.0, ua.VariantType.Double, seconds=1)
    await gateway.write("outlet_pressure", None, ua.VariantType.Double, seconds=2, status=ua.StatusCodes.BadNoCommunication)
    await until(lambda: any(r[2] == ua.StatusCodes.BadNoCommunication for r in rows(spool)))
    bad = next(r for r in rows(spool) if r[2] == ua.StatusCodes.BadNoCommunication)
    assert bad[1] is None                                                    # Bad: sem valor, mas a ausência fica registada


async def test_every_change_is_kept_even_when_they_come_in_a_burst(rig):
    gateway, collector, spool, _ = rig
    await collector.connected.wait()
    for second in range(1, 21):
        await gateway.write("outlet_pressure", 5.0 + second / 100, ua.VariantType.Double, seconds=second)
    await until(lambda: len([r for r in rows(spool) if r[1] and r[0].endswith("outlet_pressure")]) >= 20)
    values = {r[1] for r in rows(spool) if r[0].endswith("outlet_pressure")}
    assert {f"{5.0 + s / 100}" for s in range(1, 21)} <= values              # nenhuma foi fundida com outra


async def test_reconnects_after_the_gateway_dies_and_does_not_duplicate(rig):
    gateway, collector, spool, _ = rig
    await collector.connected.wait()
    await gateway.write("outlet_pressure", 5.5, ua.VariantType.Double, seconds=5)
    await until(lambda: any(r[1] == "5.5" for r in rows(spool)))
    before = len(rows(spool))
    port = gateway.port
    await gateway.stop()                                                     # o gateway cai
    await asyncio.sleep(1.2)
    assert not collector.connected.is_set()
    gateway = FakeGateway(port)                                              # e volta, com o MESMO valor e relógio
    await gateway.start()
    await gateway.write("outlet_pressure", 5.5, ua.VariantType.Double, seconds=5)
    await until(lambda: collector.sessions >= 2 and collector.connected.is_set(), timeout=15)
    await asyncio.sleep(0.5)
    again = [r for r in rows(spool) if r[1] == "5.5"]
    assert len(again) == 1                                                   # a reentrega do mesmo valor foi ignorada
    await gateway.write("outlet_pressure", 6.1, ua.VariantType.Double, seconds=6)
    await until(lambda: any(r[1] == "6.1" for r in rows(spool)))             # e as novas continuam a chegar
    await gateway.stop()


async def test_connects_when_the_gateway_appears_later(dsn):
    port = free_port()
    spool = Spool(dsn)
    collector = Collector(f"opc.tcp://127.0.0.1:{port}/ot-gateway", spool, period_ms=50, heartbeat_s=0.3, backoff_max_s=1)
    task = asyncio.create_task(collector.run())
    await asyncio.sleep(1.5)                                                 # nada a escutar: tenta e falha
    assert not collector.connected.is_set()
    gateway = FakeGateway(port)
    await gateway.start()
    await asyncio.wait_for(collector.connected.wait(), timeout=15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await gateway.stop()


async def test_samples_are_kept_in_memory_and_retried_while_the_spool_is_down(rig):
    """O ponto fraco de trocar o ficheiro local por uma base de dados em rede: se ela cair, não se perde nada."""
    import psycopg
    gateway, collector, spool, _ = rig
    await collector.connected.wait()
    real_add, falhas = spool.add, {"restam": 3}

    def spool_avariado(events):
        if falhas["restam"] > 0:
            falhas["restam"] -= 1
            raise psycopg.OperationalError("Postgres em baixo (simulado)")
        return real_add(events)

    spool.add = spool_avariado
    collector.queue.retry_s = 0.1
    for second in range(1, 6):
        await gateway.write("outlet_pressure", 7.0 + second / 100, ua.VariantType.Double, seconds=second)
    await until(lambda: falhas["restam"] == 0 and collector.queue.waiting == 0, timeout=15)
    values = {r[1] for r in rows(spool) if r[0].endswith("outlet_pressure")}
    assert {f"{7.0 + s / 100}" for s in range(1, 6)} <= values        # as 5 chegaram, apesar das 3 falhas
