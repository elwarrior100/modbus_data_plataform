"""Cenários injetáveis: agendamento, ativação em execução e leitura do ficheiro YAML."""
import itertools
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = ("drift", "sensor_stuck", "spike", "noise_burst", "comms_gap", "clock_skew", "power_cycle")
DEFAULT_DURATION = {"spike": 3.0, "power_cycle": 10.0}
NEEDS_DURATION = ("drift", "comms_gap")
NEEDS_SIGNAL = ("sensor_stuck", "spike", "noise_burst")


class ScenarioError(ValueError):
    pass


@dataclass
class Scenario:
    id: int
    asset: str                 # nome do ativo ou "*" para todos
    kind: str
    start: float               # relógio monotónico
    duration: float | None     # None = até ser cancelado
    params: dict = field(default_factory=dict)
    started: bool = False

    def _end(self) -> float | None:
        """Fim da janela ativa. No drift, `duration` é só a rampa: a falha mantém-se até ser
        cancelada (ou até `hold_s` depois do fim da rampa, se indicado)."""
        if self.kind == "drift":
            hold = self.params.get("hold_s")
            return None if hold is None else self.start + self.duration + float(hold)
        return None if self.duration is None else self.start + self.duration

    def is_active(self, now: float) -> bool:
        end = self._end()
        return self.start <= now and (end is None or now < end)

    def progress(self, now: float) -> float:
        if not self.duration:
            return 0.0
        return max(0.0, min(1.0, (now - self.start) / self.duration))

    def applies_to(self, asset: str) -> bool:
        return self.asset in ("*", asset)

    def as_dict(self, now: float) -> dict:
        return {"id": self.id, "asset": self.asset, "kind": self.kind,
                "starts_in_s": round(max(0.0, self.start - now), 1),
                "duration_s": self.duration, "params": self.params,
                "active": self.is_active(now)}


class ScenarioEngine:
    def __init__(self, assets, clock):
        self._assets = set(assets) | {"*"}
        self._clock = clock
        self._items: list[Scenario] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def activate(self, asset, kind, duration=None, params=None, start_in=0.0) -> Scenario:
        params = dict(params or {})
        if kind not in KINDS:
            raise ScenarioError(f"cenário desconhecido: {kind}")
        if asset not in self._assets:
            raise ScenarioError(f"ativo desconhecido: {asset}")
        if kind in NEEDS_SIGNAL and "signal" not in params:
            raise ScenarioError(f"{kind} requer params.signal")
        if duration is None:
            duration = DEFAULT_DURATION.get(kind)
        if kind in NEEDS_DURATION and duration is None:
            raise ScenarioError(f"{kind} requer duration_s")
        with self._lock:
            item = Scenario(next(self._ids), asset, kind, self._clock.monotonic() + start_in,
                            duration, params)
            self._items.append(item)
            return item

    def cancel(self, scenario_id: int) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [s for s in self._items if s.id != scenario_id]
            return len(self._items) < before

    def active(self, asset: str, now: float) -> list[Scenario]:
        with self._lock:
            return [s for s in self._items if s.applies_to(asset) and s.is_active(now)]

    def listing(self) -> list[dict]:
        now = self._clock.monotonic()
        with self._lock:
            return [s.as_dict(now) for s in self._items]

    def load_file(self, path: str | Path) -> int:
        data = yaml.safe_load(Path(path).read_text()) or {}
        entries = data.get("scenarios", [])
        for entry in entries:
            self.activate(entry["asset"], entry["kind"], entry.get("duration_s"),
                          entry.get("params"), start_in=float(entry.get("start_s", 0)))
        return len(entries)
