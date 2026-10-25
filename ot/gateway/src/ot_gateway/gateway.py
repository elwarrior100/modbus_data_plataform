"""Servidor OPC UA e ciclo de leitura Modbus."""
import asyncio
import logging

from asyncua import Server, ua

from .address_space import AddressSpace
from .config import GatewayConfig
from .modbus_link import ModbusLink
from .poller import DevicePoller

log = logging.getLogger("ot.gateway")
APPLICATION_URI = "urn:industrial-lake:ot-gateway"


class Gateway:
    def __init__(self, config: GatewayConfig, link: ModbusLink, port: int = 4840, host: str = "0.0.0.0"):
        self.config, self.link, self.port, self.host = config, link, port, host
        self.server = Server()
        self.space: AddressSpace | None = None
        self.pollers = [DevicePoller(d, link) for d in config.devices]

    async def start(self) -> None:
        await self.server.init()
        self.server.set_endpoint(f"opc.tcp://{self.host}:{self.port}/ot-gateway")
        self.server.set_server_name("OT Gateway")
        await self.server.set_application_uri(APPLICATION_URI)
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.space = await AddressSpace.build(self.server, self.config)
        await self.server.start()
        log.info("OPC UA a escutar em %s:%d", self.host, self.port)

    async def poll_cycle(self) -> None:
        for poller in self.pollers:
            await self.space.publish(poller.config, await poller.poll())

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await self.poll_cycle()
            await asyncio.sleep(max(0.0, self.config.poll_interval_s - (loop.time() - started)))

    async def stop(self) -> None:
        await self.server.stop()
        self.link.close()
