"""O contrato (docs/) e a configuração do gateway têm de dizer o mesmo."""
import re
from pathlib import Path

from ot_gateway.config import load_config
from ot_gateway.units import UNITS

DOCS = Path(__file__).resolve().parents[3] / "docs"
REGISTER_MAP = DOCS / "register-map.md"
ADDRESS_SPACE = DOCS / "opcua-address-space.md"
DOC_UNITS = {"m3/h": "m³/h", "degC": "°C"}   # como aparecem na tabela do mapa de registos


def test_unit_ids_match_the_documented_official_table():
    rows = re.findall(r"^\| (\S+) \| `(\w+)` \| (\d+) \| (.+?) \|$", ADDRESS_SPACE.read_text(), flags=re.M)
    assert len(rows) == len(UNITS)
    for name, code, unit_id, display in rows:
        unit = UNITS[name]
        assert (unit.code, unit.unit_id, unit.display) == (code, int(unit_id), display), name


def doc_tables():
    parsed = {}
    for section in re.split(r"^## ", REGISTER_MAP.read_text(), flags=re.M):
        match = re.match(r"\d+\. (\S+-\d+) \(unit (\d+)\) — ordem de palavras `(\w+)`", section)
        if not match:
            continue
        rows = {}
        for line in section.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 8 and cells[0][:1].isdigit():
                rows[cells[2].strip("`")] = cells
        parsed[match.group(1)] = (int(match.group(2)), match.group(3), rows)
    return parsed


def test_devices_yaml_matches_the_register_map():
    doc = doc_tables()
    config = load_config()
    assert {d.name for d in config.devices} == set(doc)
    for device in config.devices:
        unit, order, rows = doc[device.name]
        assert (device.unit, device.word_order) == (unit, order)
        assert {s.name for s in device.signals} == set(rows), device.name
        for sig in device.signals:
            addr, _ref, _name, dtype, scale, unit_txt, eurange, _scan = rows[sig.name]
            assert int(addr.split("–")[0]) == sig.addr and dtype == sig.dtype, (device.name, sig.name)
            if scale.startswith("×"):
                assert float(scale[1:]) == sig.scale, (device.name, sig.name)
            if sig.unit and sig.unit != "s":
                assert DOC_UNITS.get(sig.unit, sig.unit).replace("·", ".") in (unit_txt.replace("·", "."), unit_txt)
            if sig.eu:
                low, high = (float(x.replace(" ", "")) for x in eurange.split("–", 1))
                assert (low, high) == tuple(map(float, sig.eu)), (device.name, sig.name)


def test_alarm_coil_only_on_pump_and_motor():
    config = {d.name: d.alarm_coil for d in load_config().devices}
    assert config == {"PUMP-01": 0, "MOTOR-01": 0, "CHILLER-01": None}


def test_enum_labels_are_text_even_for_yaml_booleans(tmp_path):
    import pytest
    bad = tmp_path / "bad.yaml"
    bad.write_text("devices:\n  - {name: X, unit: 1, word_order: ABCD, signals: "
                   "[{name: state, addr: 0, type: uint16, kind: state, enum: [OFF, ON]}]}\n")
    with pytest.raises(ValueError, match="entre aspas"):
        load_config(bad)
    assert load_config().devices[2].signal("state").enum[0] == "OFF"
