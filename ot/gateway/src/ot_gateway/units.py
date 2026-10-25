"""Unidades de engenharia: código UNECE e UnitId do OPC UA.

O UnitId da tabela oficial (UNECE_to_OPCUA.csv) é o código ASCII lido como inteiro
big-endian: 'BAR' -> 0x424152 = 4342098.
"""
from dataclasses import dataclass

UNECE_NAMESPACE = "http://www.opcfoundation.org/UA/units/un/cefact"


@dataclass(frozen=True)
class Unit:
    code: str
    display: str

    @property
    def unit_id(self) -> int:
        return int.from_bytes(self.code.encode("ascii"), "big")


UNITS = {
    "bar": Unit("BAR", "bar"),
    "m3/h": Unit("MQH", "m³/h"),
    "A": Unit("AMP", "A"),
    "degC": Unit("CEL", "°C"),
    "mm/s": Unit("C16", "mm/s"),
    "rpm": Unit("RPM", "r/min"),
    "N.m": Unit("NU", "N·m"),
    "kW": Unit("KWT", "kW"),
    "h": Unit("HUR", "h"),
    "s": Unit("SEC", "s"),
}
