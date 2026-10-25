import struct

import pytest

from ot_simulator.clock import FakeClock
from ot_simulator.model import Plant
from ot_simulator.modbus import crc16, handle_pdu, handle_rtu_frame, handle_tcp_adu, with_crc
from ot_simulator.spec import DEVICES


@pytest.fixture
def plant():
    return Plant(DEVICES, FakeClock(), seed=1)


def read(plant, unit, function, start, count):
    return handle_pdu(plant, unit, struct.pack(">BHH", function, start, count))


def test_crc16_known_vector():
    assert with_crc(bytes.fromhex("01030000000A")) == bytes.fromhex("01030000000AC5CD")


def test_read_holding_returns_registers(plant):
    response = read(plant, 1, 3, 0, 2)
    assert response[0] == 3 and response[1] == 4 and len(response) == 6
    pressure = struct.unpack(">H", response[2:4])[0] * 0.01
    assert 4.5 < pressure < 5.5


def test_illegal_address_and_count(plant):
    assert read(plant, 1, 3, 100, 1) == bytes([0x83, 0x02])        # endereço fora do mapa
    assert read(plant, 1, 3, 8, 1) == bytes([0x83, 0x02])          # buraco entre sinais (7 é o último)
    assert read(plant, 1, 3, 0, 0) == bytes([0x83, 0x03])
    assert read(plant, 1, 3, 0, 126) == bytes([0x83, 0x03])


def test_write_functions_are_rejected(plant):
    assert handle_pdu(plant, 1, bytes([0x06, 0, 0, 0, 1])) == bytes([0x86, 0x01])
    assert handle_pdu(plant, 1, bytes([0x10, 0, 0, 0, 1, 2, 0, 1])) == bytes([0x90, 0x01])


def test_unknown_unit_and_offline_device_stay_silent(plant):
    assert read(plant, 9, 3, 0, 1) is None
    plant.devices[1].online = False
    assert read(plant, 1, 3, 0, 1) is None


def test_read_coils_packs_bits_lsb_first(plant):
    assert read(plant, 1, 1, 0, 1) == bytes([0x01, 0x01, 0x00])    # alarme desligado
    plant.devices[1].coils[0] = True
    assert read(plant, 1, 1, 0, 1) == bytes([0x01, 0x01, 0x01])
    assert read(plant, 3, 1, 0, 1) == bytes([0x81, 0x02])          # o chiller não tem coil


def test_rtu_frame_roundtrip_and_bad_crc(plant):
    request = with_crc(bytes([1]) + struct.pack(">BHH", 3, 0, 1))
    response = handle_rtu_frame(plant, request)
    assert response[0] == 1 and crc16(response[:-2]) == struct.unpack("<H", response[-2:])[0]
    assert handle_rtu_frame(plant, request[:-1] + b"\x00") is None


def test_tcp_adu_echoes_transaction_id(plant):
    header = struct.pack(">HHHB", 0x1234, 0, 6, 2)
    response = handle_tcp_adu(plant, header, struct.pack(">BHH", 3, 0, 2))
    assert struct.unpack(">HHHB", response[:7]) == (0x1234, 0, 7, 2)
