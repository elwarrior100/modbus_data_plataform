"""Verificação do espaço de endereços com um cliente OPC UA independente (asyncua)."""
import asyncio
import dataclasses
from datetime import timedelta

from asyncua import Client, ua

from ot_gateway.config import load_config
from ot_gateway.gateway import Gateway
from ot_gateway.modbus_link import ModbusLink

from helpers import free_port

GOOD = lambda d: d.StatusCode.is_good()           # noqa: E731
NO_COMMS = lambda d: d.StatusCode.value == ua.StatusCodes.BadNoCommunication   # noqa: E731


async def names(node):
    return {(await c.read_browse_name()).Name for c in await node.get_children()}


async def test_tree_has_assets_variables_ranges_and_units(stack):
    plant = await stack.client.nodes.objects.get_child(f"{stack.ns}:Plant")
    assert await names(plant) == {"PUMP-01", "MOTOR-01", "CHILLER-01"}
    pump = await plant.get_child(f"{stack.ns}:PUMP-01")
    assert {"Connected", "outlet_pressure", "flow_rate", "run_hours", "state", "alarm", "device_clock"} <= await names(pump)
    pressure = stack.node("PUMP-01.outlet_pressure")
    eu_range = await (await pressure.get_child(f"{stack.ns}:EURange")).read_value()
    units = await (await pressure.get_child(f"{stack.ns}:EngineeringUnits")).read_value()
    assert (eu_range.Low, eu_range.High) == (0.0, 16.0)
    assert (units.UnitId, units.DisplayName.Text) == (4342098, "bar")
    states = await (await stack.node("MOTOR-01.state").get_child(f"{stack.ns}:EnumStrings")).read_value()
    assert [s.Text for s in states][2] == "RUNNING"


async def test_values_are_good_and_decoded_with_the_right_word_order(stack):
    pressure = await stack.wait_for("PUMP-01.outlet_pressure", GOOD)
    assert 4.5 < pressure.Value.Value < 5.5
    speed = await stack.wait_for("MOTOR-01.speed_rpm", GOOD)            # uint32 em CDAB
    assert 1400 < speed.Value.Value < 1560
    power = await stack.wait_for("CHILLER-01.power_kw", GOOD)           # float32
    assert 100 < power.Value.Value < 140
    assert (await stack.wait_for("PUMP-01.state", GOOD)).Value.Value == 2
    assert (await stack.wait_for("PUMP-01.Connected", GOOD)).Value.Value is True


async def test_comms_gap_turns_variables_bad_without_a_value_then_recovers(stack):
    await stack.wait_for("CHILLER-01.inlet_temp", GOOD)
    scn = stack.plant.engine.activate("CHILLER-01", "comms_gap", duration=600)
    bad = await stack.wait_for("CHILLER-01.inlet_temp", NO_COMMS, timeout=6)
    assert bad.Value is None or bad.Value.Value is None                 # sem valor antigo
    assert (await stack.wait_for("CHILLER-01.Connected", GOOD)).Value.Value is False
    assert GOOD(await stack.read("PUMP-01.outlet_pressure"))            # os outros ativos continuam Good
    stack.plant.engine.cancel(scn.id)
    await stack.wait_for("CHILLER-01.inlet_temp", GOOD, timeout=6)
    assert (await stack.wait_for("CHILLER-01.Connected", GOOD)).Value.Value is True


async def test_clock_skew_separates_source_and_server_timestamps(stack):
    await stack.wait_for("CHILLER-01.device_clock", GOOD)
    stack.plant.engine.activate("CHILLER-01", "clock_skew", params={"seconds": 600})

    def skewed(d):
        return GOOD(d) and d.SourceTimestamp - d.ServerTimestamp > timedelta(seconds=590)

    data = await stack.wait_for("CHILLER-01.device_clock", skewed, timeout=6)
    assert timedelta(seconds=590) < data.SourceTimestamp - data.ServerTimestamp < timedelta(seconds=610)


class Collector:
    def __init__(self):
        self.values = []

    def datachange_notification(self, node, val, data):
        self.values.append(val)


async def test_subscription_deadband_filters_small_changes(stack):
    await stack.wait_for("PUMP-01.outlet_pressure", GOOD)
    node = stack.node("PUMP-01.outlet_pressure")
    wide, plain = Collector(), Collector()
    sub_wide = await stack.client.create_subscription(100, wide)
    sub_plain = await stack.client.create_subscription(100, plain)
    await sub_wide.deadband_monitor(node, 1.0)            # o ruído é ~0.05 bar: nada deve passar
    await sub_plain.subscribe_data_change(node)
    await asyncio.sleep(5)
    assert len(plain.values) >= 3                          # sem deadband, chegam as alterações
    assert len(wide.values) <= 1                           # com deadband de 1 bar, só o valor inicial
    stack.plant.engine.activate("PUMP-01", "spike", duration=3, params={"signal": "outlet_pressure", "value": 12})
    await asyncio.sleep(3)
    assert any(abs(v - 12) < 0.5 for v in wide.values)     # mas uma alteração grande passa
    await sub_wide.delete()
    await sub_plain.delete()


async def test_gateway_without_modbus_link_is_all_bad_never_good():
    config = dataclasses.replace(load_config(), poll_interval_s=0.2)
    gateway = Gateway(config, ModbusLink(f"tcp://127.0.0.1:{free_port()}", timeout=0.3),
                      port=free_port(), host="127.0.0.1")
    await gateway.start()
    task = asyncio.create_task(gateway.run_forever())
    client = Client(f"opc.tcp://127.0.0.1:{gateway.port}/ot-gateway")
    await client.connect()
    try:
        ns = await client.get_namespace_index("urn:industrial-lake:ot")
        await asyncio.sleep(1.5)
        for name in ("PUMP-01.outlet_pressure", "MOTOR-01.speed_rpm", "CHILLER-01.power_kw", "PUMP-01.alarm"):
            data = await client.get_node(ua.NodeId(name, ns)).read_data_value(raise_on_bad_status=False)
            assert data.StatusCode.is_bad(), name
            assert data.Value is None or data.Value.Value is None, name
    finally:
        await client.disconnect()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.stop()


async def test_reads_over_virtual_rs232(serial_stack):
    pressure = await serial_stack.wait_for("PUMP-01.outlet_pressure", GOOD)
    assert 4.5 < pressure.Value.Value < 5.5
    speed = await serial_stack.wait_for("MOTOR-01.speed_rpm", GOOD)
    assert 1400 < speed.Value.Value < 1560
