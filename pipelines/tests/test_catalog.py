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
