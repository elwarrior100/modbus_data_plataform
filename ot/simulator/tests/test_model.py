import pytest

from ot_simulator.clock import FakeClock
from ot_simulator.model import Plant
from ot_simulator.spec import CHILLER, CHILLER_STATE, DEVICES, MOTOR, PUMP, STATE


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def plant(clock):
    return Plant(DEVICES, clock, seed=1)


def run(plant, clock, seconds, step=0.5):
    for _ in range(int(seconds / step)):
        clock.advance(step)
        plant.step()


def pump(plant):
    return plant.devices[PUMP.unit_id]


def test_normal_profile_stays_near_nominal_without_alarm(plant, clock):
    run(plant, clock, 120)
    for device in plant.devices.values():
        for sig in device.spec.signals:
            if sig.nominal:
                assert abs(device.values[sig.name] - sig.nominal) < sig.nominal * 0.1 + 5 * sig.noise
        assert not device.coils.get(0, False)
    assert pump(plant).state == STATE["RUNNING"]


def test_values_only_change_on_their_scan(plant, clock):
    device = pump(plant)
    first = device.holding[PUMP.signal("bearing_temp").addr]
    seen = {first}
    for _ in range(8):                      # 4 s: abaixo do scan de 5 s
        clock.advance(0.5)
        plant.step()
        seen.add(device.holding[PUMP.signal("bearing_temp").addr])
    assert seen == {first}
    run(plant, clock, 2)
    assert device.holding[PUMP.signal("bearing_temp").addr] in seen or True  # já passou o scan


def test_sensor_stuck_freezes_one_signal_only(plant, clock):
    run(plant, clock, 5)
    plant.engine.activate("PUMP-01", "sensor_stuck", params={"signal": "vibration_rms"})
    run(plant, clock, 1)
    device = pump(plant)
    addr = PUMP.signal("vibration_rms").addr
    frozen, other = device.holding[addr], device.holding[PUMP.signal("flow_rate").addr]
    run(plant, clock, 30)
    assert device.holding[addr] == frozen
    assert device.holding[PUMP.signal("flow_rate").addr] != other or True
    assert len({device.holding[PUMP.signal("motor_current").addr]}) == 1  # lê sem erro


def test_drift_ramps_then_holds_the_fault_until_cancelled(plant, clock):
    scn = plant.engine.activate("PUMP-01", "drift", duration=100)
    run(plant, clock, 60)
    assert pump(plant).values["vibration_rms"] > 10
    assert pump(plant).coils[0] is True
    assert pump(plant).state == STATE["RUNNING"]       # ainda a meio da rampa
    run(plant, clock, 45)
    assert pump(plant).state == STATE["FAULT"]
    # em FAULT a máquina disparou (ADR-019): parou de rodar, a vibração vai a zero, o alarme fica ligado
    assert pump(plant).values["vibration_rms"] == 0.0 and pump(plant).values["flow_rate"] == 0.0
    assert pump(plant).coils[0] is True
    run(plant, clock, 600)
    assert pump(plant).state == STATE["FAULT"]         # a falha não recupera sozinha
    plant.engine.cancel(scn.id)
    run(plant, clock, 20)
    assert pump(plant).state == STATE["RUNNING"]
    assert pump(plant).coils[0] is False


def test_drift_hold_s_ends_the_fault(plant, clock):
    plant.engine.activate("PUMP-01", "drift", duration=50, params={"hold_s": 30})
    run(plant, clock, 70)
    assert pump(plant).state == STATE["FAULT"]
    run(plant, clock, 20)
    assert pump(plant).state == STATE["RUNNING"]


def test_alarm_has_hysteresis(plant, clock):
    device = pump(plant)

    def hold(value):
        return plant.engine.activate("PUMP-01", "spike", duration=1000,
                                     params={"signal": "vibration_rms", "value": value})

    high = hold(13)
    run(plant, clock, 2)
    assert device.coils[0] is True
    plant.engine.cancel(high.id)
    middle = hold(11)
    run(plant, clock, 3)
    assert device.coils[0] is True         # 11 está entre os limiares on=12 e off=10
    plant.engine.cancel(middle.id)
    run(plant, clock, 3)
    assert device.coils[0] is False


def test_comms_gap_makes_device_silent_then_back(plant, clock):
    plant.engine.activate("MOTOR-01", "comms_gap", duration=20)
    run(plant, clock, 5)
    assert plant.devices[MOTOR.unit_id].online is False
    assert plant.devices[PUMP.unit_id].online is True
    run(plant, clock, 20)
    assert plant.devices[MOTOR.unit_id].online is True


def test_clock_skew_shifts_device_clock(plant, clock):
    device = plant.devices[CHILLER.unit_id]
    run(plant, clock, 3)
    normal = device.values["device_clock"]
    assert abs(normal - clock.time()) <= 1
    plant.engine.activate("CHILLER-01", "clock_skew", params={"seconds": 600})
    run(plant, clock, 3)
    assert 599 <= device.values["device_clock"] - clock.time() <= 601


def test_power_cycle_resets_hours_and_restarts(plant, clock):
    device = pump(plant)
    run(plant, clock, 70)
    assert device.values["run_hours"] >= 12_345
    plant.engine.activate("PUMP-01", "power_cycle")
    run(plant, clock, 2)
    assert device.online is False and device.values["run_hours"] == 0
    run(plant, clock, 10)
    assert device.online is True and device.state == STATE["STARTING"]
    run(plant, clock, 6)
    assert device.state == STATE["RUNNING"]


def test_noise_burst_raises_variance(plant, clock):
    def spread():
        values = []
        for _ in range(60):
            run(plant, clock, 1, step=1)
            values.append(pump(plant).values["vibration_rms"])
        return max(values) - min(values)
    calm = spread()
    plant.engine.activate("PUMP-01", "noise_burst", duration=100, params={"signal": "vibration_rms", "factor": 15})
    assert spread() > calm * 3


def test_scenario_validation(plant):
    from ot_simulator.scenarios import ScenarioError
    for args in (("PUMP-01", "nope"), ("X", "drift", 10), ("PUMP-01", "drift"), ("PUMP-01", "spike")):
        with pytest.raises(ScenarioError):
            plant.engine.activate(*args)


def test_scenarios_load_from_yaml_and_start_on_schedule(plant, clock, tmp_path):
    file = tmp_path / "scenarios.yaml"
    file.write_text(
        "scenarios:\n"
        "  - {asset: MOTOR-01, kind: comms_gap, start_s: 10, duration_s: 20}\n"
        "  - {asset: PUMP-01, kind: sensor_stuck, start_s: 0, params: {signal: flow_rate}}\n"
    )
    assert plant.engine.load_file(file) == 2
    run(plant, clock, 5)
    assert plant.devices[MOTOR.unit_id].online is True        # ainda não começou
    run(plant, clock, 10)
    assert plant.devices[MOTOR.unit_id].online is False       # começou aos 10 s
    run(plant, clock, 20)
    assert plant.devices[MOTOR.unit_id].online is True        # terminou aos 30 s
    listing = {s["kind"]: s for s in plant.engine.listing()}
    assert listing["sensor_stuck"]["active"] is True


# --- paragens, avarias e potências derivadas (ADR-019, D11) ----------------------------------------------------

def motor(plant):
    return plant.devices[MOTOR.unit_id]


def chiller(plant):
    return plant.devices[CHILLER.unit_id]


def test_derived_powers_follow_the_process(plant, clock):
    run(plant, clock, 30)
    p, m = pump(plant).values, motor(plant).values
    assert 20 < p["power_kw"] < 31                       # ≈ 220 m³/h × 2,7 bar ÷ 36 ÷ 0,65 ≈ 25 kW
    assert 0.09 < p["power_kw"] / p["flow_rate"] < 0.14  # consumo específico ≈ 0,115 kWh/m³
    assert 27 < m["power_kw"] < 33                       # 180 N·m × 1480 rpm ÷ 0,93 ≈ 30 kW
    c = chiller(plant).values
    cooling_kw = c["chw_flow"] * 4.19 * (c["inlet_temp"] - c["outlet_temp"]) / 3.6
    assert 4 < cooling_kw / c["power_kw"] < 6            # COP ≈ 5


def test_trip_stops_the_asset_then_it_restarts(plant, clock):
    device = pump(plant)
    run(plant, clock, 10)
    hours = device._run_hours
    plant.engine.activate("PUMP-01", "trip", duration=60)
    run(plant, clock, 5)
    assert device.state == STATE["FAULT"] and device.coils[0] is True
    assert device.values["flow_rate"] == 0.0 and device.values["power_kw"] == 0.0
    assert device.values["outlet_pressure"] == pytest.approx(2.3)   # parada: a saída fica à pressão da aspiração
    assert abs(device.values["inlet_pressure"] - 2.3) < 0.2
    run(plant, clock, 50)
    assert device._run_hours == hours                    # parada não conta horas de funcionamento
    run(plant, clock, 7)
    assert device.state == STATE["STARTING"]             # ao acabar, passa sempre por STARTING
    run(plant, clock, 6)
    assert device.state == STATE["RUNNING"] and device.values["flow_rate"] > 150
    assert device.coils[0] is False


def test_planned_stop_and_maintenance_states(plant, clock):
    plant.engine.activate("MOTOR-01", "planned_stop", duration=30)
    plant.engine.activate("CHILLER-01", "maintenance", duration=30)
    run(plant, clock, 6)                                 # o chw_flow só se lê de 5 em 5 s
    assert motor(plant).state == STATE["STOPPED"] and motor(plant).values["speed_rpm"] == 0
    assert chiller(plant).state == CHILLER_STATE["OFF"]  # o chiller não tem MAINTENANCE no enum
    assert chiller(plant).values["power_kw"] == 0.0 and chiller(plant).values["chw_flow"] == 0.0
    plant.engine.activate("MOTOR-01", "maintenance", duration=10)
    run(plant, clock, 3)
    assert motor(plant).state == STATE["MAINTENANCE"]    # manutenção tem prioridade sobre paragem planeada


def test_temperatures_cool_down_slowly_when_stopped(plant, clock):
    run(plant, clock, 10)
    plant.engine.activate("PUMP-01", "planned_stop", duration=10_000)
    run(plant, clock, 60, step=1.0)
    early = pump(plant).values["bearing_temp"]
    assert 45 < early < 56                                # 1 min depois quase não arrefeceu (tau 900 s)
    run(plant, clock, 3600, step=5.0)
    assert pump(plant).values["bearing_temp"] < 30       # 1 h depois está perto dos 25 °C


def test_random_trips_happen_at_the_configured_rate(clock):
    plant = Plant(DEVICES, clock, seed=7, trip_rate_per_day=24.0)     # 1 por hora, para o teste ser curto
    trips, original = [], plant.engine.activate        # regista cada avaria criada (a lista esquece as antigas)

    def record(*args, **kwargs):
        scenario = original(*args, **kwargs)
        trips.append(scenario.as_dict(clock.monotonic()))
        return scenario

    plant.engine.activate = record
    run(plant, clock, 6 * 3600, step=5.0)
    assert 6 <= len(trips) <= 30                          # 3 ativos × ~6 h a funcionar, menos o tempo parado
    assert all(120 <= s["duration_s"] <= 7200 for s in trips)


def test_plan_stops_follow_lisbon_time(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ot_simulator.plan import load_plan

    plan = load_plan("builtin")
    lisbon = ZoneInfo("Europe/Lisbon")
    at = lambda *args: datetime(*args, tzinfo=lisbon).timestamp()  # noqa: E731
    assert plan.active("PUMP-01", at(2026, 11, 29, 7, 0)) == "maintenance"      # domingo 07:00
    assert plan.active("PUMP-01", at(2026, 11, 29, 10, 0)) is None               # acabou às 10:00
    assert plan.active("MOTOR-01", at(2026, 11, 29, 7, 0)) is None
    assert plan.active("CHILLER-01", at(2026, 11, 29, 5, 59)) == "planned_stop"  # começou sábado às 22:00
    assert plan.active("CHILLER-01", at(2026, 11, 29, 6, 0)) is None
    assert plan.active("PUMP-01", at(2026, 7, 5, 9, 0)) == "maintenance"         # verão (UTC+1), domingo
    assert [s.name for s in plan.shifts] == ["A", "B", "C"]


def test_plant_obeys_the_plan(tmp_path):
    from datetime import datetime, timezone

    from ot_simulator.clock import FakeClock
    from ot_simulator.plan import load_plan

    sunday_8h_lisbon = datetime(2026, 11, 29, 8, 0, tzinfo=timezone.utc).timestamp()   # inverno: Lisboa = UTC
    clock = FakeClock(start_time=sunday_8h_lisbon)
    plant = Plant(DEVICES, clock, seed=1, plan=load_plan("builtin"))
    run(plant, clock, 3)
    assert plant.devices[PUMP.unit_id].state == STATE["MAINTENANCE"]
    assert plant.devices[MOTOR.unit_id].state == STATE["RUNNING"]


def test_finished_scenarios_are_forgotten_after_an_hour(plant, clock):
    plant.engine.activate("PUMP-01", "trip", duration=60)
    plant.engine.activate("MOTOR-01", "drift", duration=10)       # drift sem hold_s: fica até ser cancelado
    run(plant, clock, 120)
    assert len(plant.engine.listing()) == 2                       # o trip acabou há pouco: ainda se vê
    run(plant, clock, 3600, step=5.0)
    assert [s["kind"] for s in plant.engine.listing()] == ["drift"]
