"""Verificação da Fase 2 contra o cluster, com um cliente OPC UA independente (asyncua).
Uso: verify_opcua.py <porto-opcua> <porto-http-simulador> <comando-congelar> <comando-retomar>"""
import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from datetime import timedelta

from asyncua import Client, ua

opcua_port, http_port, freeze_cmd, resume_cmd = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
NS_URI = "urn:industrial-lake:ot"


def check(name, ok, detail=""):
    print(f"{'OK   ' if ok else 'FALHA'} {name}: {detail}")
    if not ok:
        sys.exit(1)


def http(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{http_port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.loads(response.read())


class Collector:
    def __init__(self):
        self.values = []

    def datachange_notification(self, node, val, data):
        self.values.append(val)


async def main():
    async with Client(f"opc.tcp://127.0.0.1:{opcua_port}/ot-gateway") as client:
        ns = await client.get_namespace_index(NS_URI)
        node = lambda name: client.get_node(ua.NodeId(name, ns))   # noqa: E731
        read = lambda name: node(name).read_data_value(raise_on_bad_status=False)   # noqa: E731

        async def until(name, predicate, timeout=12):
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                data = await read(name)
                if predicate(data):
                    return data, time.monotonic() - start
                await asyncio.sleep(0.2)
            check(f"{name} (espera)", False, f"não cumprido em {timeout}s")

        plant = await client.nodes.objects.get_child(f"{ns}:Plant")
        assets = sorted([(await c.read_browse_name()).Name for c in await plant.get_children()])
        check("árvore Objects > Plant", assets == ["CHILLER-01", "MOTOR-01", "PUMP-01"], str(assets))
        pump = await plant.get_child(f"{ns}:PUMP-01")
        variables = sorted([(await c.read_browse_name()).Name for c in await pump.get_children()])
        check("variáveis da bomba", len(variables) == 10, ", ".join(variables))
        rng = await (await node("PUMP-01.outlet_pressure").get_child(f"{ns}:EURange")).read_value()
        units = await (await node("PUMP-01.outlet_pressure").get_child(f"{ns}:EngineeringUnits")).read_value()
        check("EURange e unidade", (rng.Low, rng.High, units.DisplayName.Text, units.UnitId) == (0.0, 16.0, "bar", 4342098),
              f"{rng.Low}-{rng.High} {units.DisplayName.Text} (UnitId {units.UnitId})")

        good = lambda d: d.StatusCode.is_good()   # noqa: E731
        pressure, _ = await until("PUMP-01.outlet_pressure", good)
        check("pressão da bomba Good", 4.5 < pressure.Value.Value < 5.5, f"{pressure.Value.Value} bar")
        speed, _ = await until("MOTOR-01.speed_rpm", good)
        check("uint32 do motor (CDAB, via RS-232)", 1400 < speed.Value.Value < 1560, f"{speed.Value.Value} rpm")
        power, _ = await until("CHILLER-01.power_kw", good)
        check("float32 do chiller", 100 < power.Value.Value < 140, f"{power.Value.Value} kW")

        wide, plain = Collector(), Collector()
        sub_wide, sub_plain = await client.create_subscription(100, wide), await client.create_subscription(100, plain)
        await sub_wide.deadband_monitor(node("PUMP-01.outlet_pressure"), 1.0)
        await sub_plain.subscribe_data_change(node("PUMP-01.outlet_pressure"))
        await asyncio.sleep(6)
        check("subscrição sem deadband recebe alterações", len(plain.values) >= 3, f"{len(plain.values)} notificações")
        check("subscrição com deadband de 1 bar filtra o ruído", len(wide.values) <= 1, f"{len(wide.values)} notificação(ões)")
        await sub_wide.delete()
        await sub_plain.delete()

        scn = http("POST", "/scenarios", {"asset": "CHILLER-01", "kind": "comms_gap", "duration_s": 600})
        bad, took = await until("CHILLER-01.inlet_temp", lambda d: d.StatusCode.is_bad())
        check("comms_gap: variável passa a Bad", bad.StatusCode.name == "BadNoCommunication", f"{bad.StatusCode.name} em {took:.1f}s")
        check("Bad sem valor antigo", bad.Value is None or bad.Value.Value is None, "valor omitido")
        check("outro ativo continua Good", good(await read("PUMP-01.outlet_pressure")), "PUMP-01 Good")
        http("DELETE", f"/scenarios/{scn['id']}")
        _, back = await until("CHILLER-01.inlet_temp", good)
        check("comms_gap terminou: volta a Good", True, f"em {back:.1f}s")

        subprocess.run(freeze_cmd, shell=True, check=True)
        try:
            bad, took = await until("PUMP-01.outlet_pressure", lambda d: d.StatusCode.is_bad())
            check("SIMULADOR PARADO: variável passa a Bad", bad.StatusCode.name == "BadNoCommunication", f"{bad.StatusCode.name} em {took:.1f}s")
            check("Bad sem valor antigo (simulador parado)", bad.Value is None or bad.Value.Value is None, "valor omitido")
        finally:
            subprocess.run(resume_cmd, shell=True, check=True)
        _, back = await until("PUMP-01.outlet_pressure", good)
        check("simulador retomado: volta a Good", True, f"em {back:.1f}s")

        scn = http("POST", "/scenarios", {"asset": "PUMP-01", "kind": "clock_skew", "params": {"seconds": 600}})
        skewed, _ = await until("PUMP-01.device_clock", lambda d: good(d) and d.SourceTimestamp - d.ServerTimestamp > timedelta(seconds=590))
        diff = (skewed.SourceTimestamp - skewed.ServerTimestamp).total_seconds()
        check("clock_skew: SourceTimestamp afastado do ServerTimestamp", 590 < diff < 610, f"{diff:.0f}s")
        http("DELETE", f"/scenarios/{scn['id']}")
    print("OPC UA: tudo OK")


asyncio.run(main())
