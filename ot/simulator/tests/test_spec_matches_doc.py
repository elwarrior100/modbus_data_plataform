"""O mapa de registos documentado é o contrato: o código tem de o refletir."""
import re
from pathlib import Path

from ot_simulator.registers import n_registers
from ot_simulator.spec import DEVICES

DOC = Path(__file__).resolve().parents[3] / "docs" / "register-map.md"
UNITS = {"m³/h": "m3/h", "°C": "degC", "N·m": "N.m", "—": "", "s (epoch UTC)": "s"}


def parse_doc():
    sections = re.split(r"^## ", DOC.read_text(), flags=re.M)
    parsed = {}
    for section in sections:
        match = re.match(r"\d+\. (\S+-\d+) \(unit (\d+)\) — ordem de palavras `(\w+)`", section)
        if not match:
            continue
        name, unit, order = match.groups()
        rows = {}
        for line in section.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 8 and cells[0][:1].isdigit():
                rows[cells[2].strip("`")] = cells
        parsed[name] = (int(unit), order, rows)
    return parsed


def test_every_device_is_documented():
    assert set(parse_doc()) == {d.name for d in DEVICES}


def test_devices_match_the_documented_tables():
    doc = parse_doc()
    for spec in DEVICES:
        unit, order, rows = doc[spec.name]
        assert spec.unit_id == unit
        assert spec.word_order.value == order
        assert set(rows) == {s.name for s in spec.signals}, spec.name
        for sig in spec.signals:
            addr, _ref, _name, dtype, scale, unit_txt, eurange, scan = rows[sig.name]
            first, last = int(addr.split("–")[0]), int(addr.split("–")[-1])
            assert first == sig.addr, (spec.name, sig.name)
            assert last - first + 1 == n_registers(sig.dtype), (spec.name, sig.name)
            assert dtype == sig.dtype
            if scale.startswith("×"):
                assert float(scale[1:]) == sig.scale, (spec.name, sig.name)
            assert UNITS.get(unit_txt, unit_txt) == sig.unit, (spec.name, sig.name)
            if eurange != "—" and not eurange.startswith("ver"):
                assert _range(eurange) == (sig.eu_min, sig.eu_max), (spec.name, sig.name)
            expected_scan = 0 if scan == "à mudança" else float(scan.split()[0])
            assert expected_scan == sig.scan_s, (spec.name, sig.name)


def _range(text: str):
    low, high = text.replace(" ", "").split("–", 1)
    return float(low), float(high)
