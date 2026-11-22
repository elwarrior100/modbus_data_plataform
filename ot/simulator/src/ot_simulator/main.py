"""Ponto de entrada: arranca a simulação, o Modbus TCP, o RTU (opcional) e o controlo HTTP."""
import argparse
import asyncio
import logging
import threading

from .clock import SystemClock
from .control import start_control
from .model import Plant
from .plan import load_plan
from .servers import RtuServer, start_tcp
from .spec import DEVICES

log = logging.getLogger("ot")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Simulador OT: Modbus RTU/TCP")
    p.add_argument("--tcp-port", type=int, default=5020)
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--serial-port", help="porta série do lado do escravo (ex.: /tmp/ttyV1)")
    p.add_argument("--baudrate", type=int, default=19200)
    p.add_argument("--parity", default="N", choices=["N", "E", "O"],
                   help="paridade da porta série (N no par virtual; E em linha física)")
    p.add_argument("--scenarios", help="ficheiro YAML com cenários agendados")
    p.add_argument("--seed", type=int, help="semente do ruído (reprodutível)")
    p.add_argument("--tick", type=float, default=0.1, help="período do ciclo de simulação (s)")
    p.add_argument("--plan", help='plano de produção (YAML), ou "builtin" para o que vem com o simulador')
    p.add_argument("--trip-rate-per-day", type=float, default=0.0,
                   help="avarias aleatórias por ativo e por dia a funcionar (0 = nenhuma)")
    return p


def run_ticks(plant: Plant, period: float, stop: threading.Event) -> None:
    while not stop.wait(period):
        plant.step()


async def serve(args) -> None:
    plan = load_plan(args.plan) if args.plan else None
    plant = Plant(DEVICES, SystemClock(), args.seed, plan=plan, trip_rate_per_day=args.trip_rate_per_day)
    log.info("plano: %s; avarias aleatórias: %.2f por ativo e por dia", args.plan or "nenhum", args.trip_rate_per_day)
    if args.scenarios:
        log.info("%d cenários carregados de %s", plant.engine.load_file(args.scenarios), args.scenarios)
    stop = threading.Event()
    threading.Thread(target=run_ticks, args=(plant, args.tick, stop), daemon=True, name="sim").start()
    start_control(plant, "0.0.0.0", args.http_port)
    tcp = await start_tcp(plant, "0.0.0.0", args.tcp_port)
    if args.serial_port:
        RtuServer(plant, args.serial_port, args.baudrate, args.parity).start()
    log.info("Simulador pronto: HTTP :%d", args.http_port)
    async with tcp:
        await tcp.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(serve(build_parser().parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
