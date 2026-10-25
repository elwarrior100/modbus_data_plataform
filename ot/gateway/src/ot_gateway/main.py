"""Ponto de entrada do gateway Modbus -> OPC UA."""
import argparse
import asyncio
import logging

from .config import load_config
from .gateway import Gateway
from .modbus_link import ModbusLink


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Gateway Modbus -> OPC UA")
    p.add_argument("--modbus", default="rtu:///tmp/ttyV0?baud=19200&parity=N",
                   help="tcp://host:porto ou rtu:///dev/ttyX?baud=19200&parity=N")
    p.add_argument("--opcua-port", type=int, default=4840)
    p.add_argument("--config", help="YAML de dispositivos (por omissão, o embutido)")
    p.add_argument("--timeout", type=float, default=1.0, help="timeout Modbus em segundos")
    return p


async def serve(args) -> None:
    gateway = Gateway(load_config(args.config), ModbusLink(args.modbus, args.timeout), args.opcua_port)
    await gateway.start()
    try:
        await gateway.run_forever()
    finally:
        await gateway.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    try:
        asyncio.run(serve(build_parser().parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
