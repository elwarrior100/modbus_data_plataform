"""Modelo dos dispositivos: gera valores, aplica cenários e escreve nos registos."""
import math
import random
import threading

from .plan import Plan
from .registers import encode, to_raw, from_raw
from .scenarios import STOPS, ScenarioEngine
from .spec import MOTOR_EFFICIENCY, PUMP_EFFICIENCY, DeviceSpec

SPECIAL = ("state", "device_clock")   # tratados à parte; run_hours segue o ciclo normal
STARTUP_S = 5.0
TRIP_MEAN_MIN, TRIP_MIN_MIN, TRIP_MAX_MIN = 20.0, 2.0, 120.0   # duração das avarias aleatórias (minutos)

# Sinais calculados a partir dos outros (os últimos valores escritos), para uma paragem ou uma deriva mexerem na
# potência de forma coerente. Caudal em m³/h e pressões em bar: m³/h × bar ÷ 36 = kW.
DERIVED = {
    ("PUMP-01", "power_kw"): lambda v: v["flow_rate"] * (v["outlet_pressure"] - v["inlet_pressure"]) / 36.0 / PUMP_EFFICIENCY,
    ("MOTOR-01", "power_kw"): lambda v: v["torque"] * v["speed_rpm"] * 2 * math.pi / 60.0 / 1000.0 / MOTOR_EFFICIENCY,
}


class Device:
    def __init__(self, spec: DeviceSpec, clock, rng: random.Random, engine: ScenarioEngine,
                 plan: Plan | None = None, trip_rate_per_day: float = 0.0):
        self.spec, self.clock, self.rng, self.engine = spec, clock, rng, engine
        self.plan, self.trip_rate_per_day = plan, trip_rate_per_day
        self.holding: dict[int, int] = {}
        self.coils: dict[int, bool] = {}
        self.online = True
        self.values: dict[str, float] = {}       # último valor de engenharia escrito
        self._next_scan = {s.name: 0.0 for s in spec.signals}
        self._base = {s.name: s.nominal for s in spec.signals}   # valor sem ruído dos sinais com inércia (tau_s)
        # os derivados por último: calculam-se com os valores deste mesmo passo
        self._scan_order = sorted(spec.signals, key=lambda s: (spec.name, s.name) in DERIVED)
        self._run_hours = spec.initial_run_hours
        self._last_step = clock.monotonic()
        self._starting_until = 0.0
        self._was_stopped = False
        self._alarm = False
        self.state = spec.run_state
        self.step()

    @property
    def stopped_states(self) -> tuple[int, ...]:
        s = self.spec
        return (s.fault_state, s.stopped_state, s.maintenance_state)

    @property
    def moving(self) -> bool:
        """A máquina está a rodar (a funcionar ou a arrancar)? Parada, o processo vai para os valores `idle`."""
        return self.state not in self.stopped_states

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

        self._maybe_random_trip(dt)
        self._update_state(now, drift, progress, self._stop_reason(active, wall))
        if self.state == self.spec.run_state:            # o contador só conta horas a funcionar
            self._run_hours += dt / 3600.0

        for sig in self._scan_order:
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

    def _maybe_random_trip(self, dt: float) -> None:
        """Avarias aleatórias (processo de Poisson): em média `trip_rate_per_day` por dia a funcionar."""
        if self.trip_rate_per_day <= 0 or self.state != self.spec.run_state or not self.online:
            return
        if self.rng.random() < self.trip_rate_per_day * dt / 86400.0:
            minutes = min(TRIP_MAX_MIN, max(TRIP_MIN_MIN, self.rng.expovariate(1.0 / TRIP_MEAN_MIN)))
            self.engine.activate(self.spec.name, "trip", duration=minutes * 60.0, params={"random": True})

    def _stop_reason(self, active, wall: float) -> str | None:
        """Porque é que o ativo deve estar parado agora: um cenário (por prioridade) ou o plano semanal."""
        for kind in STOPS:
            if any(s.kind == kind for s in active):
                return kind
        return self.plan.active(self.spec.name, wall) if self.plan else None

    def _update_state(self, now, drift, progress, stop: str | None) -> None:
        s = self.spec
        if (drift and progress >= 1.0) or stop == "trip":
            new = s.fault_state
        elif stop == "maintenance":
            new = s.maintenance_state
        elif stop == "planned_stop":
            new = s.stopped_state
        elif now < self._starting_until:
            new = s.starting_state
        else:
            new = s.run_state
        stopped = new in self.stopped_states
        if self._was_stopped and not stopped:            # ao sair de uma paragem passa sempre por STARTING
            self._starting_until = now + STARTUP_S
            new = s.starting_state
        self._was_stopped = stopped
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
        moving = self.moving
        derive = DERIVED.get((self.spec.name, sig.name))
        if derive:
            value = max(0.0, derive(self.values) + self.rng.gauss(0.0, sig.noise * factor)) if moving else 0.0
        elif sig.idle is not None and not moving and not sig.tau_s:
            value = sig.idle                             # parada: exatamente o valor de repouso, sem ruído
        else:
            daily = 1.0 + sig.daily_pct * math.sin(2 * math.pi * wall / 86400.0)
            target = sig.nominal * daily if (moving or sig.idle is None) else sig.idle
            if sig.tau_s:                                # inércia: aproxima-se do alvo com a constante de tempo
                self._base[sig.name] += (target - self._base[sig.name]) * (1.0 - math.exp(-sig.scan_s / sig.tau_s))
                target = self._base[sig.name]
            value = target + self.rng.gauss(0.0, sig.noise * factor)
            if moving:                                   # a deriva é desgaste: só se vê com a máquina a rodar
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

    def __init__(self, specs, clock, seed: int | None = None, plan: Plan | None = None, trip_rate_per_day: float = 0.0):
        self.clock = clock
        self.engine = ScenarioEngine([s.name for s in specs], clock)
        self.lock = threading.RLock()
        rng = random.Random(seed)
        self.devices = {s.unit_id: Device(s, clock, rng, self.engine, plan, trip_rate_per_day) for s in specs}

    def step(self) -> None:
        with self.lock:
            for device in self.devices.values():
                device.step()

    def snapshot(self) -> dict:
        with self.lock:
            return {d.spec.name: {"online": d.online, "values": dict(d.values), "state": d.state,
                                  "alarm": d.coils.get(0)} for d in self.devices.values()}
