"""Leitura de um dispositivo: blocos contíguos, descodificação e estado por sinal."""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import DeviceConfig, SignalConfig
from .modbus_link import LinkError, ModbusLink
from .registers import decode
from .status import GOOD, status_for_error, status_for_value


@dataclass
class SignalReading:
    value: float | int | bool | None
    status: int


@dataclass
class DeviceReading:
    signals: dict[str, SignalReading] = field(default_factory=dict)
    alarm: SignalReading | None = None
    connected: bool = False
    source_time: datetime | None = None


def plan_blocks(device: DeviceConfig) -> list[tuple[int, int]]:
    """Intervalos contíguos de registos a ler: (endereço inicial, quantidade)."""
    spans = sorted((s.addr, s.addr + s.n_registers) for s in device.signals)
    blocks: list[list[int]] = []
    for start, end in spans:
        if blocks and start <= blocks[-1][1]:
            blocks[-1][1] = max(blocks[-1][1], end)
        else:
            blocks.append([start, end])
    return [(a, b - a) for a, b in blocks]


def convert(signal: SignalConfig, regs: list[int], word_order: str):
    raw = decode(signal.dtype, regs, word_order)
    if signal.kind in ("counter", "state", "clock"):
        return int(raw)
    if signal.dtype == "float32":
        return round(raw * signal.scale, 3)
    return round(raw * signal.scale, 6)


class DevicePoller:
    def __init__(self, config: DeviceConfig, link: ModbusLink):
        self.config, self.link = config, link
        self.blocks = plan_blocks(config)

    async def poll(self) -> DeviceReading:
        cfg, reading = self.config, DeviceReading(connected=True)
        for start, count in self.blocks:
            try:
                regs = await self.link.read_holding(cfg.unit, start, count)
            except LinkError as error:
                reading.connected = False
                for sig in cfg.signals:
                    if start <= sig.addr < start + count:
                        reading.signals[sig.name] = SignalReading(None, status_for_error(error))
                continue
            for sig in cfg.signals:
                if start <= sig.addr < start + count:
                    value = convert(sig, regs[sig.addr - start: sig.addr - start + sig.n_registers], cfg.word_order)
                    reading.signals[sig.name] = SignalReading(value, status_for_value(sig, value) if sig.kind == "analog" else GOOD)
        if cfg.alarm_coil is not None:
            try:
                bit = (await self.link.read_coils(cfg.unit, cfg.alarm_coil, 1))[0]
                reading.alarm = SignalReading(bool(bit), GOOD)
            except LinkError as error:
                reading.connected = False
                reading.alarm = SignalReading(None, status_for_error(error))
        clock = next((s for s in cfg.signals if s.kind == "clock"), None)
        if clock and (r := reading.signals.get(clock.name)) and r.value is not None:
            reading.source_time = datetime.fromtimestamp(r.value, tz=timezone.utc)
        return reading
