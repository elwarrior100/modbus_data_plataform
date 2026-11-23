"""O "dia sintético" do gold: ~2 dias de bronze com o simulador real e o plano de produção (gold/plan.yaml), avarias
`trip` e uma falha de comunicação curta, já passados a silver num lakeFS em memória.

Calendário (Lisboa = UTC em novembro): sábado 31-10 e domingo 1-11 de 2026. O gold calcula-se para os dois dias, por
ordem (o domingo usa o estado final do sábado). O que lá acontece, e o que o gold tem de mostrar:
- plano: chiller parado de sábado 22:00 a domingo 06:00; manutenção da bomba domingo 06–10 e do motor 10–12;
- bomba: avaria de 20 min a atravessar a meia-noite (sáb. 23:50 → dom. 00:10) e outra de 15 min no domingo às 14:00;
- motor: avaria de 20 min no domingo às 18:00 e 10 min sem comunicação às 20:00 (passa o G2 com aviso);
- chiller: avaria de 10 min no domingo às 15:00.
"""
from datetime import datetime, timedelta, timezone

from fake_lake import FakeLake
from fixture_bronze import bronze_files, generate
from ot_simulator.plan import load_plan as load_sim_plan
from silver.run import GateFailed, run_hour

START = datetime(2026, 10, 31, tzinfo=timezone.utc)         # sábado
SATURDAY, SUNDAY = START.date(), (START + timedelta(days=1)).date()
HOURS = 50                                                  # até domingo + 2 h: a janela do gold de domingo vai até 01h


def _at(hours: float) -> float:
    return hours * 3600


def incidents(plant) -> None:
    a = plant.engine.activate
    a("PUMP-01", "trip", duration=1200, start_in=_at(23 + 50 / 60))          # sáb. 23:50 → dom. 00:10
    a("PUMP-01", "trip", duration=900, start_in=_at(24 + 14))                # dom. 14:00
    a("CHILLER-01", "trip", duration=600, start_in=_at(24 + 15))             # dom. 15:00
    a("MOTOR-01", "trip", duration=1200, start_in=_at(24 + 18))              # dom. 18:00
    a("MOTOR-01", "comms_gap", duration=600, start_in=_at(24 + 20))          # dom. 20:00, 10 min


def make_silver_lake(poll_s: int = 30) -> tuple[FakeLake, list, set[str]]:
    """(lago com bronze e silver no main, eventos de bronze, horas recusadas pelo G2)."""
    events = generate(start=START, incidents=incidents, plan=load_sim_plan("builtin"), seconds=HOURS * 3600, poll_s=poll_s)
    lake = FakeLake()
    lake.load_bronze(bronze_files(events))
    blocked = set()
    for h in range(HOURS + 1):
        hour = START + timedelta(hours=h)
        try:
            run_hour(lake, hour)
        except GateFailed:
            blocked.add(hour.strftime("%Y-%m-%dT%H"))
    return lake, events, blocked
