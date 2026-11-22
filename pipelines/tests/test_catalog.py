from pathlib import Path

import yaml

from silver.catalog import load_catalog

GATEWAY = Path(__file__).resolve().parents[2] / "ot" / "gateway" / "src" / "ot_gateway" / "devices.yaml"
UNIT = {"bar": "BAR", "m3/h": "MQH", "A": "AMP", "degC": "CEL", "mm/s": "C16", "rpm": "RPM", "N.m": "NU", "kW": "KWT", "h": "HUR", "s": "SEC"}


def test_catalog_matches_the_gateway_config():
    expected = {}
    for device in yaml.safe_load(GATEWAY.read_text())["devices"]:
        expected[f"ns=2;s={device['name']}.Connected"] = ("boolean", None, None)
        for s in device["signals"]:
            kind = s.get("kind", "analog")
            expected[f"ns=2;s={device['name']}.{s['name']}"] = (kind, UNIT.get(s.get("unit", "")) if kind != "clock" else None,
                                                               tuple(map(float, s["eu"])) if "eu" in s else None)
        if device.get("alarm_coil") is not None:
            expected[f"ns=2;s={device['name']}.alarm"] = ("boolean", None, None)
    got = {t.node_id: (t.kind, t.unit, (t.eu_min, t.eu_max) if t.eu_min is not None else None) for t in load_catalog()}
    assert got == expected


def test_state_tags_carry_their_labels():
    states = {t.node_id: t.enum for t in load_catalog() if t.kind == "state"}
    assert states["ns=2;s=MOTOR-01.state"][2] == "RUNNING"
    assert states["ns=2;s=CHILLER-01.state"][0] == "OFF"      # 'OFF' tem de ser texto, não o booleano False


def test_moves_with_run_mirrors_the_simulator():
    """`moves_with_run` = no simulador o sinal tem valor de repouso fixo (idle sem tau_s) ou é uma potência derivada:
    parado, fica constante e sem amostras. O G2-06 depende disto (ADR-019, D11)."""
    from ot_simulator.model import DERIVED
    from ot_simulator.spec import DEVICES

    expected = {(d.name, s.name) for d in DEVICES for s in d.signals
                if (s.idle is not None and not s.tau_s) or (d.name, s.name) in DERIVED}
    assert {(t.asset, t.tag) for t in load_catalog() if t.moves_with_run} == expected
