"""Servidores de transporte: Modbus TCP (asyncio) e Modbus RTU sobre porta série (thread)."""
import asyncio
import logging
import struct
import threading

import serial

from .modbus import handle_rtu_frame, handle_tcp_adu

log = logging.getLogger("ot.servers")


async def start_tcp(plant, host: str, port: int):
    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                header = await reader.readexactly(7)
                length = struct.unpack(">H", header[4:6])[0]
                body = await reader.readexactly(length - 1)
                response = handle_tcp_adu(plant, header, body)
                if response is not None:        # silêncio = o cliente fica em timeout
                    writer.write(response)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(client, host, port)
    log.info("Modbus TCP a escutar em %s:%d", host, port)
    return server


class RtuServer(threading.Thread):
    """Lê tramas da porta série, delimitadas por silêncio na linha, e responde."""

    def __init__(self, plant, port: str, baudrate: int = 19200, parity: str = "N"):
        super().__init__(daemon=True, name="rtu-server")
        self.plant = plant
        self.port = serial.Serial(port, baudrate, bytesize=8, parity=parity, stopbits=1, timeout=0.2)
        self.gap = max(0.003, 3.5 * 11 / baudrate)     # silêncio entre tramas (3.5 caracteres)
        self._halt = threading.Event()

    def run(self) -> None:
        log.info("Modbus RTU a escutar em %s", self.port.port)
        while not self._halt.is_set():
            first = self.port.read(1)
            if not first:
                continue
            frame = bytearray(first)
            self.port.timeout = self.gap * 4
            while chunk := self.port.read(256):
                frame += chunk
            self.port.timeout = 0.2
            response = handle_rtu_frame(self.plant, bytes(frame))
            if response is not None:
                self.port.write(response)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=1)
        self.port.close()
