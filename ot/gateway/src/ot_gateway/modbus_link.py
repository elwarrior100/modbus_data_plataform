"""Ligação de mestre Modbus (RTU sobre série ou TCP) com erros classificados."""
import asyncio
from urllib.parse import parse_qs, urlparse

from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.framer import FramerType


class LinkError(Exception):
    pass


class NoResponse(LinkError):
    """O dispositivo não respondeu (timeout, linha cortada, ligação perdida)."""


class ModbusExceptionReply(LinkError):
    """O dispositivo respondeu com uma exceção Modbus."""

    def __init__(self, code: int):
        super().__init__(f"exceção Modbus 0x{code:02X}")
        self.code = code


def build_client(url: str, timeout: float):
    """`tcp://host:porto` ou `rtu:///dev/ttyV0?baud=19200&parity=N`."""
    parsed = urlparse(url)
    if parsed.scheme == "tcp":
        return AsyncModbusTcpClient(parsed.hostname, port=parsed.port or 502, timeout=timeout, retries=0)
    if parsed.scheme == "rtu":
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return AsyncModbusSerialClient(parsed.path, framer=FramerType.RTU, baudrate=int(query.get("baud", 19200)),
                                       bytesize=8, parity=query.get("parity", "N"), stopbits=1,
                                       timeout=timeout, retries=0)
    raise ValueError(f"URL Modbus não suportado: {url}")


class ModbusLink:
    def __init__(self, url: str, timeout: float = 1.0):
        self.url, self.timeout = url, timeout
        self._client = build_client(url, timeout)

    async def _connected(self) -> None:
        if not self._client.connected and not await self._client.connect():
            raise NoResponse(f"sem ligação a {self.url}")

    async def _call(self, method: str, unit: int, address: int, count: int):
        try:
            await self._connected()
            response = await getattr(self._client, method)(address, count=count, device_id=unit)
        except (ModbusException, asyncio.TimeoutError, OSError) as exc:
            raise NoResponse(str(exc)) from exc
        if response.isError():
            raise ModbusExceptionReply(getattr(response, "exception_code", 0))
        return response

    async def read_holding(self, unit: int, address: int, count: int) -> list[int]:
        return list((await self._call("read_holding_registers", unit, address, count)).registers)

    async def read_coils(self, unit: int, address: int, count: int) -> list[bool]:
        return list((await self._call("read_coils", unit, address, count)).bits[:count])

    def close(self) -> None:
        self._client.close()
