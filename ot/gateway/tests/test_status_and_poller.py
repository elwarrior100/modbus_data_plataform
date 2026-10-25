import pytest
from asyncua import ua

from ot_gateway.config import load_config
from ot_gateway.modbus_link import LinkError, ModbusExceptionReply, NoResponse
from ot_gateway.poller import DevicePoller, plan_blocks
from ot_gateway.status import is_bad, status_for_error, status_for_value

CONFIG = {d.name: d for d in load_config().devices}


class FakeLink:
    """Mestre falso: devolve registos de um dicionário, ou levanta o erro configurado."""

    def __init__(self, regs=None, coils=None, fail=None):
        self.regs, self.coils, self.fail = regs or {}, coils or {}, fail or {}

    async def read_holding(self, unit, address, count):
        error = self.fail.get((unit, address))
        if error:
            raise error
        return [self.regs[(unit, a)] for a in range(address, address + count)]

    async def read_coils(self, unit, address, count):
        error = self.fail.get((unit, "coil"))
        if error:
            raise error
        return [self.coils.get((unit, address), False)]


def test_no_response_is_bad_never_good():
    status = status_for_error(NoResponse("timeout"))
    assert status == ua.StatusCodes.BadNoCommunication and is_bad(status)


@pytest.mark.parametrize("code,expected", [
    (0x02, ua.StatusCodes.BadConfigurationError), (0x04, ua.StatusCodes.BadDeviceFailure),
    (0x06, ua.StatusCodes.BadDeviceFailure), (0x01, ua.StatusCodes.BadCommunicationError)])
def test_modbus_exceptions_map_to_distinct_bad_codes(code, expected):
    assert status_for_error(ModbusExceptionReply(code)) == expected


def test_value_outside_eurange_is_uncertain_not_good():
    pressure = CONFIG["PUMP-01"].signal("outlet_pressure")
    assert status_for_value(pressure, 5.0) == ua.StatusCodes.Good
    assert status_for_value(pressure, 20.0) == ua.StatusCodes.UncertainEngineeringUnitsExceeded


def test_blocks_are_contiguous_ranges():
    assert plan_blocks(CONFIG["PUMP-01"]) == [(0, 8), (20, 2)]
    assert plan_blocks(CONFIG["CHILLER-01"]) == [(0, 5), (20, 2)]


def pump_link(**kw):
    regs = {(1, 0): 507, (1, 1): 2204, (1, 2): 421, (1, 3): 0xFFC9, (1, 4): 251,
            (1, 5): 0x0001, (1, 6): 0x86A0, (1, 7): 2, (1, 20): 0x6A0C, (1, 21): 0x3A80}
    return FakeLink(regs, {(1, 0): True}, **kw)


async def test_poll_scales_decodes_and_reports_good():
    reading = await DevicePoller(CONFIG["PUMP-01"], pump_link()).poll()
    assert reading.connected is True
    values = {k: v.value for k, v in reading.signals.items()}
    assert values["outlet_pressure"] == 5.07 and values["flow_rate"] == 220.4
    assert values["bearing_temp"] == -5.5                    # int16 negativo
    assert values["run_hours"] == 100000                      # uint32 ABCD
    assert values["state"] == 2
    assert all(r.status == ua.StatusCodes.Good for r in reading.signals.values())
    assert reading.alarm.value is True
    assert reading.source_time is not None


async def test_motor_uint32_uses_cdab_order():
    regs = {(2, 0): 0x05C8, (2, 1): 0x0000, (2, 2): 1800, (2, 3): 400, (2, 4): 750, (2, 5): 200, (2, 6): 2,
            (2, 20): 0x0000, (2, 21): 0x6A0C}
    reading = await DevicePoller(CONFIG["MOTOR-01"], FakeLink(regs, {})).poll()
    assert reading.signals["speed_rpm"].value == 1480    # [0x05C8, 0x0000] em CDAB


async def test_timeout_makes_every_signal_bad_with_no_value():
    link = pump_link(fail={(1, 0): NoResponse("x"), (1, 20): NoResponse("x"), (1, "coil"): NoResponse("x")})
    reading = await DevicePoller(CONFIG["PUMP-01"], link).poll()
    assert reading.connected is False
    assert reading.source_time is None
    for name, item in reading.signals.items():
        assert item.status == ua.StatusCodes.BadNoCommunication and item.value is None, name
    assert reading.alarm.status == ua.StatusCodes.BadNoCommunication


async def test_one_failing_block_only_degrades_its_own_signals():
    link = pump_link(fail={(1, 20): ModbusExceptionReply(0x02)})
    reading = await DevicePoller(CONFIG["PUMP-01"], link).poll()
    assert reading.connected is False
    assert reading.signals["outlet_pressure"].status == ua.StatusCodes.Good
    assert reading.signals["device_clock"].status == ua.StatusCodes.BadConfigurationError
