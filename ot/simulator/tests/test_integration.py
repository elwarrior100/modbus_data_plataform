"""Verificação do mapa de registos com um cliente independente (pymodbus)."""
import time

import pytest
from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.exceptions import ModbusIOException
from pymodbus.framer import FramerType

DT = ModbusTcpClient.DATATYPE


def tcp_client(live):
    client = ModbusTcpClient("127.0.0.1", port=live.tcp_port, timeout=0.6, retries=0)
    assert client.connect()
    return client


def serial_client(live):
    if not live.serial_client_port:
        pytest.skip("socat não está instalado")
    client = ModbusSerialClient(live.serial_client_port, framer=FramerType.RTU, baudrate=19200,
                                parity="N", timeout=0.6, retries=0)
    assert client.connect()
    return client


@pytest.fixture(params=["tcp", "rtu"])
def client(request, live):
    c = tcp_client(live) if request.param == "tcp" else serial_client(live)
    yield c
    c.close()


def regs(client, unit, address, count):
    response = client.read_holding_registers(address, count=count, device_id=unit)
    assert not response.isError(), response
    return response.registers


def test_pump_pressure_is_about_5_bar_on_both_transports(client):
    pressure = regs(client, 1, 0, 1)[0] * 0.01
    assert 4.5 < pressure < 5.5


def test_motor_speed_needs_cdab_word_order(client):
    registers = regs(client, 2, 0, 2)
    right = client.convert_from_registers(registers, DT.UINT32, word_order="little")
    wrong = client.convert_from_registers(registers, DT.UINT32, word_order="big")
    assert 1400 < right < 1560
    assert not 0 < wrong < 3600            # a ordem errada dá um valor absurdo, sem erro


def test_pump_run_hours_reads_with_abcd(client):
    registers = regs(client, 1, 5, 2)
    assert client.convert_from_registers(registers, DT.UINT32, word_order="big") >= 12_345


def test_chiller_power_is_float32(client):
    power = client.convert_from_registers(regs(client, 3, 2, 2), DT.FLOAT32, word_order="big")
    assert 100 < power < 140


def test_device_clock_matches_wall_clock(client):
    seconds = client.convert_from_registers(regs(client, 2, 20, 2), DT.UINT32, word_order="little")
    assert abs(seconds - time.time()) < 5


def test_unmapped_address_is_an_exception_not_a_value(client):
    assert client.read_holding_registers(100, count=1, device_id=1).isError()


def test_writes_are_refused(client):
    assert client.write_register(0, 1, device_id=1).isError()


def test_http_control_lists_activates_and_cancels(live):
    status, created = live.http("POST", "/scenarios", {"asset": "CHILLER-01", "kind": "spike",
                                                       "duration_s": 1, "params": {"signal": "power_kw"}})
    assert status == 201 and created["kind"] == "spike"
    assert any(s["id"] == created["id"] for s in live.http("GET", "/scenarios")[1])
    assert live.http("DELETE", f"/scenarios/{created['id']}")[1] == {"cancelled": True}
    assert live.http("GET", "/health")[1] == {"status": "ok"}


def test_sensor_stuck_is_observable_from_outside(live):
    client = tcp_client(live)
    try:
        def samples(n):
            out = []
            for _ in range(n):
                out.append(regs(client, 1, 4, 1)[0])
                time.sleep(0.35)
            return out
        assert len(set(samples(8))) > 1                      # normalmente a vibração varia
        scn = live.http("POST", "/scenarios", {"asset": "PUMP-01", "kind": "sensor_stuck",
                                               "params": {"signal": "vibration_rms"}})[1]
        time.sleep(1.2)
        frozen = samples(8)
        assert len(set(frozen)) == 1                         # congelada
        assert len(set(regs(client, 1, 3, 1))) >= 1          # o resto do ativo continua a responder
        live.http("DELETE", f"/scenarios/{scn['id']}")
    finally:
        client.close()


def test_drift_turns_the_alarm_coil_on(live):
    client = tcp_client(live)
    try:
        assert client.read_coils(0, count=1, device_id=2).bits[0] is False
        scn = live.http("POST", "/scenarios", {"asset": "MOTOR-01", "kind": "drift", "duration_s": 3})[1]
        time.sleep(4.5)
        assert client.read_coils(0, count=1, device_id=2).bits[0] is True
        live.http("DELETE", f"/scenarios/{scn['id']}")
    finally:
        client.close()


def test_comms_gap_is_silence_not_an_error_reply(live):
    for make in (tcp_client, serial_client):
        client = make(live)
        try:
            scn = live.http("POST", "/scenarios", {"asset": "CHILLER-01", "kind": "comms_gap",
                                                   "duration_s": 60})[1]
            time.sleep(0.4)
            with pytest.raises(ModbusIOException):          # timeout: nada foi respondido
                client.read_holding_registers(0, count=1, device_id=3)
            assert not client.read_holding_registers(0, count=1, device_id=1).isError()  # outro ativo responde
            live.http("DELETE", f"/scenarios/{scn['id']}")
            time.sleep(0.4)
            assert not client.read_holding_registers(0, count=1, device_id=3).isError()
        finally:
            client.close()
