"""7 dias de bronze realista, gerados com o modelo do simulador e um relógio acelerado.
Imita o gateway: publica uma variável só quando o valor ou o estado muda, e marca Bad quando o dispositivo cai.
Injeta um incidente de cada tipo, para cada aviso da silver ter onde se exercitar."""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ingest import bronze
from ingest.envelope import Event, make_batch_id, to_us
from ot_simulator.clock import FakeClock
from ot_simulator.model import Plant
from ot_simulator.spec import DEVICES
from silver.catalog import load_catalog

START = datetime(2026, 11, 1, tzinfo=timezone.utc)
GOOD, UNCERTAIN_EU, BAD_NO_COMM = 0x00000000, 0x40940000, 0x80310000


def _at(day: int, hour: int) -> float:
    return day * 86400 + hour * 3600


def _incidents(plant: Plant) -> None:
    a = plant.engine.activate
    a("PUMP-01", "spike", duration=90, params={"signal": "outlet_pressure", "value": 20}, start_in=_at(0, 3))      # fora do intervalo
    a("CHILLER-01", "comms_gap", duration=3600, start_in=_at(1, 10))                                              # dispositivo cala-se 1 h
    a("MOTOR-01", "clock_skew", duration=7200, params={"seconds": 900}, start_in=_at(2, 12))                      # relógio 15 min adiantado
    a("PUMP-01", "power_cycle", start_in=_at(3, 8))                                                               # reinício
    a("PUMP-01", "drift", duration=6 * 3600, params={"hold_s": 3600}, start_in=_at(4, 6))                         # avaria progressiva
    a("MOTOR-01", "sensor_stuck", duration=3600, params={"signal": "vibration_rms"}, start_in=_at(5, 9))         # sensor parado


def generate(days: int = 7, poll_s: int = 30, seed: int = 42) -> list[Event]:
    clock = FakeClock(start_time=START.timestamp())
    plant = Plant(DEVICES, clock, seed=seed)
    _incidents(plant)
    tags = {t.node_id: t for t in load_catalog()}
    last: dict[str, tuple] = {}
    events: list[Event] = []
    seen: set[str] = set()                                       # como o spool: event_id repetido é ignorado

    def publish(node, raw, status, source_dt):
        if last.get(node) == (raw, status):                      # o gateway só escreve quando muda
            return
        last[node] = (raw, status)
        now = datetime.fromtimestamp(clock.time(), timezone.utc)
        ev = Event.build("ot-gateway", node, raw, status, to_us(source_dt), to_us(now),
                         to_us(now + timedelta(milliseconds=50)))
        if ev.event_id in seen:      # ex.: o relógio do dispositivo volta atrás ao fim do clock_skew e repete source_ts+valor
            return
        seen.add(ev.event_id)
        events.append(ev)

    def poll():
        now = datetime.fromtimestamp(clock.time(), timezone.utc)
        for dev in plant.devices.values():
            name, spec = dev.spec.name, dev.spec
            publish(f"ns=2;s={name}.Connected", json.dumps(dev.online), GOOD, now)
            device_time = datetime.fromtimestamp(dev.values["device_clock"], timezone.utc)
            for sig in spec.signals:
                node = f"ns=2;s={name}.{sig.name}"
                tag = tags[node]
                if not dev.online:
                    publish(node, None, BAD_NO_COMM, now)
                    continue
                value, status = dev.values[sig.name], GOOD
                if tag.kind == "clock":
                    raw = json.dumps(datetime.fromtimestamp(value, timezone.utc).isoformat())
                elif tag.kind in ("counter", "state"):
                    raw = json.dumps(int(value))
                else:
                    value = round(value, 3 if sig.dtype == "float32" else 6)
                    raw = json.dumps(value)
                    if tag.eu_min is not None and not tag.eu_min <= value <= tag.eu_max:
                        status = UNCERTAIN_EU
                publish(node, raw, status, device_time)
            if spec.has_alarm_coil:
                node = f"ns=2;s={name}.alarm"
                publish(node, json.dumps(bool(dev.coils.get(0))) if dev.online else None, GOOD if dev.online else BAD_NO_COMM,
                        device_time if dev.online else now)

    poll()
    for _ in range(days * 86400 // poll_s):
        clock.advance(poll_s)
        plant.step()
        poll()
    return events


def bronze_files(events: list[Event]) -> dict[str, bytes]:
    """Um ficheiro de bronze por hora de receção: caminho → bytes."""
    by_hour = defaultdict(list)
    for e in events:
        by_hour[bronze.partition_of(e)].append(e)
    files = {}
    for rows in by_hour.values():
        batch_id = make_batch_id([e.event_id for e in rows])
        (path, part), = bronze.partitions(rows, batch_id).items()
        files[path] = bronze.to_parquet(bronze.to_table(part, batch_id))
    return files
