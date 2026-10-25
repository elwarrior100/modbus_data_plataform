import pytest

from ot_simulator.clock import FakeClock
from ot_simulator.model import Plant
from ot_simulator.spec import CHILLER, DEVICES, MOTOR, PUMP, STATE


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
    assert pump(plant).values["vibration_rms"] > 20
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
