"""Verificação da Fase 1 por RS-232 virtual, a correr DENTRO do pod, no lado /tmp/ttyV0."""
from pymodbus.client import ModbusSerialClient
from pymodbus.framer import FramerType

client = ModbusSerialClient("/tmp/ttyV0", framer=FramerType.RTU, baudrate=19200, parity="N",
                            timeout=1, retries=0)
assert client.connect(), "sem ligação à porta série"
DT = client.DATATYPE


def regs(unit, addr, count):
    r = client.read_holding_registers(addr, count=count, device_id=unit)
    assert not r.isError(), r
    return r.registers


pressure = regs(1, 0, 1)[0] * 0.01
assert 4.5 < pressure < 5.5, pressure
print(f"OK   RS-232 lê pressão da bomba: {pressure:.2f} bar")
rpm = client.convert_from_registers(regs(2, 0, 2), DT.UINT32, word_order="little")
assert 1400 < rpm < 1560, rpm
print(f"OK   RS-232 uint32 do motor com CDAB: {rpm} rpm")
power = client.convert_from_registers(regs(3, 2, 2), DT.FLOAT32, word_order="big")
assert 100 < power < 140, power
print(f"OK   RS-232 float32 do chiller: {power:.1f} kW")
print("RS-232: tudo OK")
