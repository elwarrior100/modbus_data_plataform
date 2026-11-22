"""Plano de produção: turnos e paragens planeadas semanais, em hora local (ADR-019, D11 F6).

O simulador cumpre as paragens do plano (o ativo fica STOPPED ou MAINTENANCE nessas janelas) e o gold usa o mesmo
plano para separar o tempo planeado do não planeado. As horas são de Lisboa; numa mudança de hora uma janela conta
pela hora de parede (das 06:00 às 10:00 locais, dure isso 3, 4 ou 5 horas reais).
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
STOP_KINDS = ("planned_stop", "maintenance")


@dataclass(frozen=True)
class Shift:
    name: str
    start: time


@dataclass(frozen=True)
class PlannedStop:
    asset: str
    kind: str                # planned_stop | maintenance
    weekday: int             # 0 = segunda
    start: time
    hours: float


@dataclass(frozen=True)
class Plan:
    tz: ZoneInfo
    shifts: tuple[Shift, ...]
    stops: tuple[PlannedStop, ...]

    def active(self, asset: str, wall: float) -> str | None:
        """O tipo da paragem planeada em curso para o ativo neste instante (epoch), ou None."""
        local = datetime.fromtimestamp(wall, self.tz)
        for stop in self.stops:
            if stop.asset != asset:
                continue
            for back in range(int(stop.hours // 24) + 2):           # a janela pode ter começado em dias anteriores
                day = local.date() - timedelta(days=back)
                if day.weekday() != stop.weekday:
                    continue
                begin = datetime.combine(day, stop.start, self.tz)
                if begin <= local < begin + timedelta(hours=stop.hours):
                    return stop.kind
        return None


def _time(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def load_plan(path: str | Path) -> Plan:
    """`path` é um ficheiro YAML, ou "builtin" para o plano que vem com o simulador (plan.yaml)."""
    text = (resources.files("ot_simulator").joinpath("plan.yaml").read_text() if str(path) == "builtin"
            else Path(path).read_text())
    data = yaml.safe_load(text)
    shifts = tuple(Shift(s["name"], _time(s["start"])) for s in data["shifts"])
    stops = []
    for s in data.get("planned_stops", []):
        if s["kind"] not in STOP_KINDS:
            raise ValueError(f"paragem planeada com tipo inválido: {s['kind']}")
        stops.append(PlannedStop(s["asset"], s["kind"], WEEKDAYS.index(s["weekday"]), _time(s["start"]), float(s["hours"])))
    return Plan(ZoneInfo(data["timezone"]), shifts, tuple(stops))
