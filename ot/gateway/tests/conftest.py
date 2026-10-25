import asyncio
import dataclasses
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import pytest_asyncio
from asyncua import Client, Node, ua

from helpers import free_port
from ot_gateway.address_space import NAMESPACE_URI
from ot_gateway.config import load_config
from ot_gateway.gateway import Gateway
from ot_gateway.modbus_link import ModbusLink
from ot_simulator.clock import SystemClock
from ot_simulator.model import Plant
from ot_simulator.servers import RtuServer, start_tcp
from ot_simulator.spec import DEVICES


@dataclass
class Stack:
    plant: Plant
    gateway: Gateway
    client: Client
    ns: int

    def node(self, name: str) -> Node:
        return self.client.get_node(ua.NodeId(name, self.ns))

    async def read(self, name: str) -> ua.DataValue:
        return await self.node(name).read_data_value(raise_on_bad_status=False)

    async def wait_for(self, name: str, predicate, timeout: float = 8.0) -> ua.DataValue:
        deadline = time.monotonic() + timeout
        while True:
            data = await self.read(name)
            if predicate(data):
                return data
            assert time.monotonic() < deadline, f"{name}: condição não cumprida em {timeout}s ({data})"
            await asyncio.sleep(0.15)


async def build_stack(transport: str, tmp_path=None):
    plant = Plant(DEVICES, SystemClock(), seed=3)
    halt = threading.Event()

    def ticks():
        while not halt.wait(0.05):
            plant.step()

    threading.Thread(target=ticks, daemon=True).start()
    cleanup = [halt.set]
    if transport == "tcp":
        tcp = await start_tcp(plant, "127.0.0.1", 0)
        url = f"tcp://127.0.0.1:{tcp.sockets[0].getsockname()[1]}"
        cleanup.append(tcp.close)
    else:
        master, slave = str(tmp_path / "ttyV0"), str(tmp_path / "ttyV1")
        socat = subprocess.Popen(["socat", f"pty,raw,echo=0,link={master}", f"pty,raw,echo=0,link={slave}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if (tmp_path / "ttyV0").exists() and (tmp_path / "ttyV1").exists():
                break
            time.sleep(0.05)
        rtu = RtuServer(plant, slave)
        rtu.start()
        url = f"rtu://{master}?baud=19200&parity=N"
        cleanup += [rtu.stop, socat.terminate]
    config = dataclasses.replace(load_config(), poll_interval_s=0.3)
    gateway = Gateway(config, ModbusLink(url, timeout=0.5), port=free_port(), host="127.0.0.1")
    await gateway.start()
    task = asyncio.create_task(gateway.run_forever())
    client = Client(f"opc.tcp://127.0.0.1:{gateway.port}/ot-gateway")
    await client.connect()
    stack = Stack(plant, gateway, client, await client.get_namespace_index(NAMESPACE_URI))
    return stack, task, cleanup


async def teardown(stack, task, cleanup):
    await stack.client.disconnect()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await stack.gateway.stop()
    for fn in cleanup:
        fn()


@pytest_asyncio.fixture
async def stack():
    s, task, cleanup = await build_stack("tcp")
    yield s
    await teardown(s, task, cleanup)


@pytest_asyncio.fixture
async def serial_stack(tmp_path):
    if not shutil.which("socat"):
        import pytest
        pytest.skip("socat não está instalado")
    s, task, cleanup = await build_stack("rtu", tmp_path)
    yield s
    await teardown(s, task, cleanup)
