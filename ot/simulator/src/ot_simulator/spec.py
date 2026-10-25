"""Especificação dos dispositivos. Espelha docs/register-map.md (um teste compara os dois)."""
from dataclasses import dataclass, field

from .registers import WordOrder

STATE = {"STOPPED": 0, "STARTING": 1, "RUNNING": 2, "FAULT": 3, "MAINTENANCE": 4}
CHILLER_STATE = {"OFF": 0, "STANDBY": 1, "COOLING": 2, "FAULT": 3}


@dataclass(frozen=True)
class Signal:
    name: str
    addr: int
    dtype: str
    scale: float
    unit: str
    eu_min: float
    eu_max: float
    scan_s: float            # 0 = só quando muda
    nominal: float = 0.0     # perfil de funcionamento normal
    noise: float = 0.0
    daily_pct: float = 0.0


@dataclass(frozen=True)
class AlarmRule:
    signal: str
    on: float
    off: float


@dataclass(frozen=True)
class DeviceSpec:
    name: str
    unit_id: int
    word_order: WordOrder
    signals: tuple[Signal, ...]
    alarm_rules: tuple[AlarmRule, ...] = ()
    state_values: dict = field(default_factory=lambda: dict(STATE))
    run_state: int = STATE["RUNNING"]
    fault_state: int = STATE["FAULT"]
    starting_state: int = STATE["STARTING"]
    drift_deltas: dict = field(default_factory=dict)   # variação máxima do cenário drift
    initial_run_hours: float = 12_345.0

    def signal(self, name: str) -> Signal:
        return next(s for s in self.signals if s.name == name)

    @property
    def has_alarm_coil(self) -> bool:
        return bool(self.alarm_rules)


CLOCK = lambda: Signal("device_clock", 20, "uint32", 1, "s", 0, 4294967295, 1)  # noqa: E731

PUMP = DeviceSpec(
    name="PUMP-01", unit_id=1, word_order=WordOrder.ABCD,
    signals=(
        Signal("outlet_pressure", 0, "uint16", 0.01, "bar", 0, 16, 1, 5.0, 0.05),
        Signal("flow_rate", 1, "uint16", 0.1, "m3/h", 0, 500, 1, 220, 3),
        Signal("motor_current", 2, "uint16", 0.1, "A", 0, 100, 1, 42, 0.5),
        Signal("bearing_temp", 3, "int16", 0.1, "degC", -20, 150, 5, 55, 0.3, 0.02),
        Signal("vibration_rms", 4, "uint16", 0.01, "mm/s", 0, 50, 1, 2.5, 0.15),
        Signal("run_hours", 5, "uint32", 1, "h", 0, 4294967295, 60),
        Signal("state", 7, "uint16", 1, "", 0, 4, 0),
        CLOCK(),
    ),
    alarm_rules=(AlarmRule("vibration_rms", 12, 10), AlarmRule("bearing_temp", 90, 85)),
    drift_deltas={"vibration_rms": 25.0, "bearing_temp": 50.0},
)

MOTOR = DeviceSpec(
    name="MOTOR-01", unit_id=2, word_order=WordOrder.CDAB,
    signals=(
        Signal("speed_rpm", 0, "uint32", 1, "rpm", 0, 3600, 1, 1480, 4),
        Signal("torque", 2, "int16", 0.1, "N.m", -500, 1000, 1, 180, 2),
        Signal("current", 3, "uint16", 0.1, "A", 0, 200, 1, 40, 0.5),
        Signal("winding_temp", 4, "int16", 0.1, "degC", -20, 200, 5, 75, 0.4, 0.02),
        Signal("vibration_rms", 5, "uint16", 0.01, "mm/s", 0, 50, 1, 2.0, 0.12),
        Signal("state", 6, "uint16", 1, "", 0, 4, 0),
        CLOCK(),
    ),
    alarm_rules=(AlarmRule("vibration_rms", 10, 8), AlarmRule("winding_temp", 120, 110)),
    drift_deltas={"vibration_rms": 20.0, "winding_temp": 60.0},
)

CHILLER = DeviceSpec(
    name="CHILLER-01", unit_id=3, word_order=WordOrder.ABCD,
    signals=(
        Signal("inlet_temp", 0, "int16", 0.1, "degC", 0, 40, 5, 12.0, 0.1, 0.02),
        Signal("outlet_temp", 1, "int16", 0.1, "degC", 0, 30, 5, 7.0, 0.1, 0.02),
        Signal("power_kw", 2, "float32", 1, "kW", 0, 500, 1, 120, 2, 0.02),
        Signal("state", 4, "uint16", 1, "", 0, 3, 0),
        CLOCK(),
    ),
    state_values=dict(CHILLER_STATE),
    run_state=CHILLER_STATE["COOLING"],
    fault_state=CHILLER_STATE["FAULT"],
    starting_state=CHILLER_STATE["STANDBY"],
)

DEVICES = (PUMP, MOTOR, CHILLER)
