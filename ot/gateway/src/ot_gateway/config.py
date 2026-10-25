"""Configuração dos dispositivos: o espelho do mapa de registos que o gateway precisa de saber."""
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from .registers import SIZES

KINDS = ("analog", "counter", "state", "clock")


@dataclass(frozen=True)
class SignalConfig:
    name: str
    addr: int
    dtype: str
    scale: float = 1.0
    unit: str = ""
    eu: tuple[float, float] | None = None
    kind: str = "analog"
    enum: tuple[str, ...] = ()

    @property
    def n_registers(self) -> int:
        return SIZES[self.dtype]


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    unit: int
    word_order: str
    signals: tuple[SignalConfig, ...]
    alarm_coil: int | None = None

    def signal(self, name: str) -> SignalConfig:
        return next(s for s in self.signals if s.name == name)


@dataclass(frozen=True)
class GatewayConfig:
    poll_interval_s: float
    devices: tuple[DeviceConfig, ...] = field(default_factory=tuple)


def _enum_labels(device: str, signal: dict) -> tuple[str, ...]:
    labels = tuple(signal.get("enum", ()))
    for label in labels:
        if not isinstance(label, str):   # OFF, ON, NO, YES sem aspas viram booleanos em YAML 1.1
            raise ValueError(f"{device}.{signal['name']}: etiqueta {label!r} não é texto; ponha-a entre aspas")
    return labels


def load_config(path: str | Path | None = None) -> GatewayConfig:
    text = Path(path).read_text() if path else resources.files("ot_gateway").joinpath("devices.yaml").read_text()
    data = yaml.safe_load(text)
    devices = []
    for d in data["devices"]:
        signals = []
        for s in d["signals"]:
            kind = s.get("kind", "analog")
            if kind not in KINDS:
                raise ValueError(f"{d['name']}.{s['name']}: kind inválido {kind}")
            signals.append(SignalConfig(
                name=s["name"], addr=s["addr"], dtype=s["type"], scale=float(s.get("scale", 1)),
                unit=s.get("unit", ""), eu=tuple(s["eu"]) if "eu" in s else None, kind=kind,
                enum=_enum_labels(d["name"], s)))
        devices.append(DeviceConfig(d["name"], d["unit"], d["word_order"], tuple(signals), d.get("alarm_coil")))
    return GatewayConfig(float(data.get("poll_interval_s", 1.0)), tuple(devices))
