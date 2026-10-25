"""Verificação da Fase 1 por Modbus TCP, com cliente independente (pymodbus).
Uso: verify_tcp.py <host> <porto-modbus> <porto-http>"""
import json
import sys
import time
import urllib.request

from pymodbus.client import ModbusTcpClient

host, modbus_port, http_port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
DT = ModbusTcpClient.DATATYPE
client = ModbusTcpClient(host, port=modbus_port, timeout=1, retries=0)
assert client.connect(), "sem ligação Modbus TCP"


def regs(unit, addr, count):
    r = client.read_holding_registers(addr, count=count, device_id=unit)
    assert not r.isError(), r
    return r.registers


def http(method, path, body=None):
    req = urllib.request.Request(f"http://{host}:{http_port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read())


def check(name, ok, detail):
    print(f"{'OK  ' if ok else 'FALHA'} {name}: {detail}")
    if not ok:
        sys.exit(1)


pressure = regs(1, 0, 1)[0] * 0.01
check("TCP lê pressão da bomba", 4.5 < pressure < 5.5, f"{pressure:.2f} bar")

rpm_regs = regs(2, 0, 2)
right = client.convert_from_registers(rpm_regs, DT.UINT32, word_order="little")
wrong = client.convert_from_registers(rpm_regs, DT.UINT32, word_order="big")
check("uint32 do motor com CDAB", 1400 < right < 1560, f"{right} rpm")
check("mesma leitura com ABCD é absurda", not 0 < wrong < 3600, f"{wrong}")

hours = client.convert_from_registers(regs(1, 5, 2), DT.UINT32, word_order="big")
check("uint32 da bomba com ABCD (run_hours)", hours >= 12_345, f"{hours} h")

power = client.convert_from_registers(regs(3, 2, 2), DT.FLOAT32, word_order="big")
check("float32 do chiller", 100 < power < 140, f"{power:.1f} kW")

samples = []
for _ in range(8):
    samples.append(regs(1, 4, 1)[0]); time.sleep(0.35)
check("vibração normalmente varia", len(set(samples)) > 1, f"{len(set(samples))} valores distintos")

scn = http("POST", "/scenarios", {"asset": "PUMP-01", "kind": "sensor_stuck", "params": {"signal": "vibration_rms"}})
time.sleep(1.5)
frozen = []
for _ in range(8):
    frozen.append(regs(1, 4, 1)[0]); time.sleep(0.35)
check("sensor_stuck congela a vibração", len(set(frozen)) == 1, f"sempre {frozen[0]}")
others = {regs(1, 1, 1)[0] for _ in range(1)}
check("o resto do ativo continua a responder", bool(others), f"flow_rate={others.pop() * 0.1:.1f} m3/h")
http("DELETE", f"/scenarios/{scn['id']}")
print("TCP: tudo OK")
