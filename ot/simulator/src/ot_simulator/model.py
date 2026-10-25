"""Modelo dos dispositivos: gera valores, aplica cenários e escreve nos registos."""
import math
import random
import threading

from .registers import encode, to_raw, from_raw
from .scenarios import ScenarioEngine
from .spec import DeviceSpec

SPECIAL = ("state", "device_clock")   # tratados à parte; run_hours segue o ciclo normal
STARTUP_S = 5.0


class Device:
    def __init__(self, spec: DeviceSpec, clock, rng: random.Random, engine: ScenarioEngine):
        self.spec, self.clock, self.rng, self.engine = spec, clock, rng, engine
        self.holding: dict[int, int] = {}
        self.coils: dict[int, bool] = {}
        self.online = True
        self.values: dict[str, float] = {}       # último valor de engenharia escrito
        self._next_scan = {s.name: 0.0 for s in spec.signals}
        self._run_hours = spec.initial_run_hours
        self._last_step = clock.monotonic()
        self._starting_until = 0.0
        self._alarm = False
        self.state = spec.run_state
        self.step()

    # --- escrita nos registos -------------------------------------------------
    def _write(self, name: str, value: float) -> None:
        sig = self.spec.signal(name)
        raw = to_raw(sig.dtype, value, sig.scale)
        for offset, word in enumerate(encode(sig.dtype, raw, self.spec.word_order)):
            self.holding[sig.addr + offset] = word
        self.values[name] = from_raw(sig.dtype, raw, sig.scale)

    # --- passo de simulação ---------------------------------------------------
    def step(self) -> None:
        now, wall = self.clock.monotonic(), self.clock.time()
        dt, self._last_step = now - self._last_step, now
        active = self.engine.active(self.spec.name, now)
        for scn in active:
            if not scn.started:
                scn.started = True
                self._on_start(scn, now)
        by_kind = lambda kind: [s for s in active if s.kind == kind]  # noqa: E731

        self.online = not (by_kind("comms_gap") or by_kind("power_cycle"))
        drift = by_kind("drift")
        progress = max((s.progress(now) for s in drift), default=0.0)

        self._update_state(now, drift, progress)
        self._run_hours += dt / 3600.0

        for sig in self.spec.signals:
            if sig.name in SPECIAL or now < self._next_scan[sig.name]:
                continue
            self._next_scan[sig.name] = now + sig.scan_s
            self._scan_signal(sig, active, wall, progress)

        if self._due("device_clock", now):
            skew = sum(s.params.get("seconds", 300) + s.params.get("drift_s_per_s", 0.0) * (now - s.start)
                       for s in by_kind("clock_skew"))
            self._write("device_clock", int(wall + skew))
        if self.spec.has_alarm_coil:
            self._update_alarm()

    def _due(self, name: str, now: float) -> bool:
        if now < self._next_scan[name]:
            return False
        self._next_scan[name] = now + self.spec.signal(name).scan_s
        return True

    def _on_start(self, scn, now: float) -> None:
        if scn.kind == "power_cycle":
            self._starting_until = now + (scn.duration or 0.0) + STARTUP_S
            if any(sig.name == "run_hours" for sig in self.spec.signals):
                self._run_hours = 0.0
                self._write("run_hours", 0)

    def _update_state(self, now, drift, progress) -> None:
        if drift and progress >= 1.0:
            new = self.spec.fault_state
        elif now < self._starting_until:
            new = self.spec.starting_state
        else:
            new = self.spec.run_state
        if new != self.state or "state" not in self.values:
            self.state = new
            self._write("state", new)

    def _scan_signal(self, sig, active, wall, progress) -> None:
        if sig.name == "run_hours":
            self._write("run_hours", int(self._run_hours))
            return
        if any(s.kind == "sensor_stuck" and s.params["signal"] == sig.name for s in active) \
                and sig.name in self.values:
            return
        factor = 1.0
        for s in active:
            if s.kind == "noise_burst" and s.params["signal"] == sig.name:
                factor *= float(s.params.get("factor", 10))
        daily = 1.0 + sig.daily_pct * math.sin(2 * math.pi * wall / 86400.0)
        value = sig.nominal * daily + self.rng.gauss(0.0, sig.noise * factor)
        value += progress * self.spec.drift_deltas.get(sig.name, 0.0)
        for s in active:
            if s.kind == "spike" and s.params["signal"] == sig.name:
                value = float(s.params.get("value", sig.nominal * 3))
        self._write(sig.name, value)

    def _update_alarm(self) -> None:
        values = self.values
        triggered = any(values.get(r.signal, 0.0) >= r.on for r in self.spec.alarm_rules)
        cleared = all(values.get(r.signal, 0.0) < r.off for r in self.spec.alarm_rules)
        if triggered or self.state == self.spec.fault_state:
            self._alarm = True
        elif cleared:
            self._alarm = False
        self.coils[0] = self._alarm


class Plant:
    """Conjunto dos dispositivos, acedido pelos servidores e pelo ciclo de simulação."""

    def __init__(self, specs, clock, seed: int | None = None):
        self.clock = clock
        self.engine = ScenarioEngine([s.name for s in specs], clock)
        self.lock = threading.RLock()
        rng = random.Random(seed)
        self.devices = {s.unit_id: Device(s, clock, rng, self.engine) for s in specs}

    def step(self) -> None:
        with self.lock:
            for device in self.devices.values():
                device.step()

    def snapshot(self) -> dict:
        with self.lock:
            return {d.spec.name: {"online": d.online, "values": dict(d.values),
                                  "alarm": d.coils.get(0)} for d in self.devices.values()}
