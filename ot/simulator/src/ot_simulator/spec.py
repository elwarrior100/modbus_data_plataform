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
    # Ativo parado (STOPPED, FAULT, MAINTENANCE): `idle` é o valor para onde o sinal vai (None = não depende do estado).
    # Com `tau_s` = 0 o valor é exatamente `idle`, sem ruído (um caudalímetro parado mostra 0,0); com `tau_s` > 0 o
    # sinal aproxima-se de `idle` (ou do nominal, ao voltar a funcionar) com essa constante de tempo, como uma
    # temperatura a arrefecer, e mantém o ruído.
    idle: float | None = None
    tau_s: float = 0.0


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
    stopped_state: int = STATE["STOPPED"]
    maintenance_state: int = STATE["MAINTENANCE"]
    drift_deltas: dict = field(default_factory=dict)   # variação máxima do cenário drift
    initial_run_hours: float = 12_345.0

    def signal(self, name: str) -> Signal:
        return next(s for s in self.signals if s.name == name)

    @property
    def has_alarm_coil(self) -> bool:
        return bool(self.alarm_rules)


AMBIENT = 25.0               # °C: para onde arrefecem as temperaturas de um ativo parado

# Rendimentos usados nas potências derivadas (o simulador inventa a física; os valores são plausíveis):
PUMP_EFFICIENCY = 0.65       # global, do fio à água: 220 m³/h × 2,7 bar ÷ 0,65 ≈ 25 kW (≈ 0,115 kWh/m³)
MOTOR_EFFICIENCY = 0.93      # 180 N·m × 1480 rpm ≈ 27,9 kW mecânicos ≈ 30 kW elétricos

CLOCK = lambda: Signal("device_clock", 20, "uint32", 1, "s", 0, 4294967295, 1)  # noqa: E731

PUMP = DeviceSpec(
    name="PUMP-01", unit_id=1, word_order=WordOrder.ABCD,
    signals=(
        Signal("outlet_pressure", 0, "uint16", 0.01, "bar", 0, 16, 1, 5.0, 0.05, idle=2.3),   # parada: = aspiração
        Signal("flow_rate", 1, "uint16", 0.1, "m3/h", 0, 500, 1, 220, 3, idle=0.0),
        Signal("motor_current", 2, "uint16", 0.1, "A", 0, 100, 1, 42, 0.5, idle=0.0),
        Signal("bearing_temp", 3, "int16", 0.1, "degC", -20, 150, 5, 55, 0.3, 0.02, idle=AMBIENT, tau_s=900),
        Signal("vibration_rms", 4, "uint16", 0.01, "mm/s", 0, 50, 1, 2.5, 0.15, idle=0.0),
        Signal("run_hours", 5, "uint32", 1, "h", 0, 4294967295, 60),
        Signal("state", 7, "uint16", 1, "", 0, 4, 0),
        # derivado (DERIVED em model.py): potência elétrica = caudal × (saída − aspiração) ÷ rendimento global
        Signal("power_kw", 8, "float32", 1, "kW", 0, 100, 1, 0.0, 0.2),
        Signal("inlet_pressure", 10, "uint16", 0.01, "bar", 0, 10, 5, 2.3, 0.02),   # aspiração: não depende da bomba
        CLOCK(),
    ),
    alarm_rules=(AlarmRule("vibration_rms", 12, 10), AlarmRule("bearing_temp", 90, 85)),
    drift_deltas={"vibration_rms": 25.0, "bearing_temp": 50.0},
)

MOTOR = DeviceSpec(
    name="MOTOR-01", unit_id=2, word_order=WordOrder.CDAB,
    signals=(
        Signal("speed_rpm", 0, "uint32", 1, "rpm", 0, 3600, 1, 1480, 4, idle=0.0),
        Signal("torque", 2, "int16", 0.1, "N.m", -500, 1000, 1, 180, 2, idle=0.0),
        Signal("current", 3, "uint16", 0.1, "A", 0, 200, 1, 40, 0.5, idle=0.0),
        Signal("winding_temp", 4, "int16", 0.1, "degC", -20, 200, 5, 75, 0.4, 0.02, idle=AMBIENT, tau_s=1200),
        Signal("vibration_rms", 5, "uint16", 0.01, "mm/s", 0, 50, 1, 2.0, 0.12, idle=0.0),
        Signal("state", 6, "uint16", 1, "", 0, 4, 0),
        # derivado: potência elétrica = binário × velocidade angular ÷ rendimento do motor
        Signal("power_kw", 7, "float32", 1, "kW", 0, 100, 1, 0.0, 0.2),
        CLOCK(),
    ),
    alarm_rules=(AlarmRule("vibration_rms", 10, 8), AlarmRule("winding_temp", 120, 110)),
    drift_deltas={"vibration_rms": 20.0, "winding_temp": 60.0},
)

CHILLER = DeviceSpec(
    name="CHILLER-01", unit_id=3, word_order=WordOrder.ABCD,
    signals=(
        Signal("inlet_temp", 0, "int16", 0.1, "degC", 0, 40, 5, 12.0, 0.1, 0.02),
        Signal("outlet_temp", 1, "int16", 0.1, "degC", 0, 30, 5, 7.0, 0.1, 0.02, idle=12.0, tau_s=600),  # parado: = entrada
        Signal("power_kw", 2, "float32", 1, "kW", 0, 500, 1, 120, 2, 0.02, idle=0.0),
        Signal("state", 4, "uint16", 1, "", 0, 3, 0),
        # caudal de água gelada: com o ΔT dá a potência de frio (≈ 103 × 4,19 × 5 ÷ 3,6 ≈ 600 kW, COP ≈ 5)
        Signal("chw_flow", 5, "uint16", 0.1, "m3/h", 0, 300, 5, 103, 1, idle=0.0),
        CLOCK(),
    ),
    state_values=dict(CHILLER_STATE),
    run_state=CHILLER_STATE["COOLING"],
    fault_state=CHILLER_STATE["FAULT"],
    starting_state=CHILLER_STATE["STANDBY"],
    stopped_state=CHILLER_STATE["OFF"],
    maintenance_state=CHILLER_STATE["OFF"],      # o enum do chiller não tem MAINTENANCE: fica OFF
)

DEVICES = (PUMP, MOTOR, CHILLER)
