import asyncio
import json
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass

import pytest

from ot_simulator.clock import SystemClock
from ot_simulator.control import start_control
from ot_simulator.model import Plant
from ot_simulator.servers import RtuServer, start_tcp
from ot_simulator.spec import DEVICES


@dataclass
class Live:
    plant: Plant
    tcp_port: int
    http_port: int
    serial_client_port: str | None

    def http(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.http_port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    plant = Plant(DEVICES, SystemClock(), seed=7)
    stop = threading.Event()

    def ticks():
        while not stop.wait(0.05):
            plant.step()

    threading.Thread(target=ticks, daemon=True).start()
    loop = asyncio.new_event_loop()
    holder = {}

    def serve():
        asyncio.set_event_loop(loop)
        holder["server"] = loop.run_until_complete(start_tcp(plant, "127.0.0.1", 0))
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    while "server" not in holder:
        time.sleep(0.01)
    tcp_port = holder["server"].sockets[0].getsockname()[1]
    http = start_control(plant, "127.0.0.1", 0)

    socat, rtu, client_port = None, None, None
    if shutil.which("socat"):
        base = tmp_path_factory.mktemp("tty")
        master, slave = str(base / "ttyV0"), str(base / "ttyV1")
        socat = subprocess.Popen(["socat", f"pty,raw,echo=0,link={master}", f"pty,raw,echo=0,link={slave}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if (base / "ttyV0").exists() and (base / "ttyV1").exists():
                break
            time.sleep(0.05)
        rtu = RtuServer(plant, slave)
        rtu.start()
        client_port = master

    yield Live(plant, tcp_port, http.server_address[1], client_port)

    stop.set()
    if rtu:
        rtu.stop()
    if socat:
        socat.terminate()
    http.shutdown()
    loop.call_soon_threadsafe(loop.stop)
