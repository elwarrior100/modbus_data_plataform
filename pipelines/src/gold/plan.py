"""O plano de produção visto pelo gold: turnos e paragens planeadas, já convertidos para UTC (ADR-019, D3 e D11 F6).

O gold agrega por hora UTC; o negócio fala em turnos e dias de Lisboa. Este módulo faz a ponte:
- cada hora UTC cai num só turno (a mudança de hora é sempre de 1 h inteira e os turnos começam em horas certas);
- o turno C (22:00–06:00) pertence ao dia em que começa: a "data de produção" das horas entre a meia-noite e as 06:00
  é a do dia anterior;
- as paragens planeadas contam pela hora de parede, como no simulador (das 06:00 às 10:00 locais, dure isso 3, 4 ou
  5 horas reais num dia de mudança de hora).
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from importlib import resources
from zoneinfo import ZoneInfo

import yaml

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
UTC = timezone.utc


@dataclass(frozen=True)
class Stop:
    asset: str
    kind: str                # planned_stop | maintenance
    weekday: int             # 0 = segunda
    start: time
    hours: float


@dataclass(frozen=True)
class Period:
    """Um turno ou um dia de produção, com o início e o fim em UTC."""
    production_date: date
    shift: str | None        # None = o dia inteiro (A + B + C)
    start: datetime
    end: datetime

    @property
    def hours(self) -> int:
        return int((self.end - self.start).total_seconds() // 3600)


@dataclass(frozen=True)
class Plan:
    tz: ZoneInfo
    shifts: tuple[tuple[str, time], ...]          # por ordem de início
    stops: tuple[Stop, ...]

    def _local(self, day: date, at: time) -> datetime:
        return datetime.combine(day, at, self.tz)

    def shift_of(self, hour_utc: datetime) -> tuple[date, str]:
        """(data de produção, turno) da hora UTC que começa em `hour_utc`."""
        local = hour_utc.astimezone(self.tz)
        name = None
        for shift, start in self.shifts:
            if local.time() >= start:
                name = shift
        if name is None:                          # antes do 1.º turno do dia: é o último turno do dia anterior
            return local.date() - timedelta(days=1), self.shifts[-1][0]
        return local.date(), name

    def shift_periods(self, production_date: date) -> list[Period]:
        """Os turnos de uma data de produção, em UTC. O fim de um é o início do seguinte (hora de parede)."""
        out = []
        for i, (name, start) in enumerate(self.shifts):
            begin = self._local(production_date, start)
            nxt_day, nxt_start = (production_date, self.shifts[i + 1][1]) if i + 1 < len(self.shifts) \
                else (production_date + timedelta(days=1), self.shifts[0][1])
            out.append(Period(production_date, name, begin.astimezone(UTC), self._local(nxt_day, nxt_start).astimezone(UTC)))
        return out

    def day_period(self, production_date: date) -> Period:
        shifts = self.shift_periods(production_date)
        return Period(production_date, None, shifts[0].start, shifts[-1].end)

    def periods_ending_in(self, day: date) -> list[Period]:
        """Os turnos e dias de produção que ACABAM no dia UTC `day` (fim em ]00:00, 24:00]). Um KPI fica na partição do
        dia em que o período acaba: é a primeira em que já tem todas as horas."""
        lo = datetime.combine(day, time(0), UTC)
        hi = lo + timedelta(days=1)
        out = []
        for d in (day - timedelta(days=2), day - timedelta(days=1), day, day + timedelta(days=1)):
            for p in self.shift_periods(d) + [self.day_period(d)]:
                if lo < p.end <= hi:
                    out.append(p)
        return sorted(out, key=lambda p: (p.end, p.shift is None, p.shift or ""))

    def stop_windows(self, start: datetime, end: datetime) -> list[tuple[str, str, datetime, datetime]]:
        """(ativo, tipo, início UTC, fim UTC) das paragens planeadas que tocam [start, end)."""
        out = []
        first, last = start.astimezone(self.tz).date() - timedelta(days=2), end.astimezone(self.tz).date() + timedelta(days=1)
        d = first
        while d <= last:
            for s in self.stops:
                if d.weekday() != s.weekday:
                    continue
                begin = self._local(d, s.start)
                finish = begin + timedelta(hours=s.hours)      # aritmética "de parede" (mesmo tzinfo), como no simulador
                b, e = begin.astimezone(UTC), finish.astimezone(UTC)
                if b < end and e > start:
                    out.append((s.asset, s.kind, b, e))
            d += timedelta(days=1)
        return sorted(out)


def _time(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def load_plan(text: str | None = None) -> Plan:
    """O plano do pacote (`gold/plan.yaml`), ou o YAML em `text`."""
    data = yaml.safe_load(text if text is not None else resources.files("gold").joinpath("plan.yaml").read_text())
    shifts = tuple(sorted(((s["name"], _time(s["start"])) for s in data["shifts"]), key=lambda s: s[1]))
    stops = tuple(Stop(s["asset"], s["kind"], WEEKDAYS.index(s["weekday"]), _time(s["start"]), float(s["hours"]))
                  for s in data.get("planned_stops", []))
    return Plan(ZoneInfo(data["timezone"]), shifts, stops)
