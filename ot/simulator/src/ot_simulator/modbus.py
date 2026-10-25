"""Protocolo Modbus (escravo): funções 0x01 e 0x03, enquadramento RTU e TCP."""
import struct

READ_COILS, READ_HOLDING = 0x01, 0x03
ILLEGAL_FUNCTION, ILLEGAL_ADDRESS, ILLEGAL_VALUE = 0x01, 0x02, 0x03
MAX_REGISTERS, MAX_COILS = 125, 2000


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(frame: bytes) -> bytes:
    return frame + struct.pack("<H", crc16(frame))


def exception(function: int, code: int) -> bytes:
    return bytes([function | 0x80, code])


def handle_pdu(plant, unit: int, pdu: bytes) -> bytes | None:
    """Devolve o PDU de resposta, ou None se o dispositivo não responde (ausente ou offline)."""
    with plant.lock:
        device = plant.devices.get(unit)
        if device is None or not device.online or not pdu:
            return None
        function = pdu[0]
        if function not in (READ_COILS, READ_HOLDING):
            return exception(function, ILLEGAL_FUNCTION)
        if len(pdu) != 5:
            return exception(function, ILLEGAL_VALUE)
        start, count = struct.unpack(">HH", pdu[1:5])
        if function == READ_HOLDING:
            return _read_holding(device, start, count)
        return _read_coils(device, start, count)


def _read_holding(device, start: int, count: int) -> bytes:
    if not 1 <= count <= MAX_REGISTERS:
        return exception(READ_HOLDING, ILLEGAL_VALUE)
    addresses = range(start, start + count)
    if any(a not in device.holding for a in addresses):
        return exception(READ_HOLDING, ILLEGAL_ADDRESS)
    data = b"".join(struct.pack(">H", device.holding[a]) for a in addresses)
    return bytes([READ_HOLDING, len(data)]) + data


def _read_coils(device, start: int, count: int) -> bytes:
    if not 1 <= count <= MAX_COILS:
        return exception(READ_COILS, ILLEGAL_VALUE)
    addresses = range(start, start + count)
    if any(a not in device.coils for a in addresses):
        return exception(READ_COILS, ILLEGAL_ADDRESS)
    packed = bytearray((count + 7) // 8)
    for i, address in enumerate(addresses):
        if device.coils[address]:
            packed[i // 8] |= 1 << (i % 8)
    return bytes([READ_COILS, len(packed)]) + bytes(packed)


def handle_rtu_frame(plant, frame: bytes) -> bytes | None:
    """Trama RTU completa (unit + PDU + CRC) -> trama de resposta, ou None."""
    if len(frame) < 4 or crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
        return None
    response = handle_pdu(plant, frame[0], frame[1:-2])
    return None if response is None else with_crc(bytes([frame[0]]) + response)


def handle_tcp_adu(plant, header: bytes, body: bytes) -> bytes | None:
    """MBAP (7 bytes) + PDU -> ADU de resposta, ou None."""
    transaction, protocol, _length, unit = struct.unpack(">HHHB", header)
    if protocol != 0:
        return None
    response = handle_pdu(plant, unit, body)
    if response is None:
        return None
    return struct.pack(">HHHB", transaction, 0, len(response) + 1, unit) + response
