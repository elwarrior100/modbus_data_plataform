"""Linha de comandos: collect, write, status e inject-bad."""
import argparse
import asyncio
import logging
import os
import sys
import time

from .bronze import load_nodes
from .collector import Collector
from .envelope import Event
from .lake import Lake
from .live import try_install
from .spool import Spool
from .writer import Writer

env = os.environ.get


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ingest", description="Ingestão: OPC UA -> spool -> bronze no lakeFS")
    p.add_argument("--database-url", default=env("DATABASE_URL"), help="PostgreSQL do spool (ou a variável DATABASE_URL)")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="subscrever o gateway OPC UA e guardar no spool")
    c.add_argument("--opcua", default=env("OPCUA_URL", "opc.tcp://ot-edge.ot.svc.cluster.local:4840/ot-gateway"))
    w = sub.add_parser("write", help="escrever os lotes do spool no bronze do lakeFS")
    w.add_argument("--endpoint", default=env("LAKEFS_ENDPOINT", "http://lakefs.data.svc.cluster.local:8000"))
    w.add_argument("--repository", default=env("LAKEFS_REPOSITORY", "industrial-lake"))
    w.add_argument("--prefix", default=env("BRONZE_PREFIX", "bronze"))
    w.add_argument("--target", default=env("TARGET_BRANCH", "main"))
    w.add_argument("--batch-rows", type=int, default=int(env("BATCH_ROWS", "5000")))
    w.add_argument("--batch-age", type=float, default=float(env("BATCH_AGE_S", "60")))
    w.add_argument("--interval", type=float, default=float(env("WRITE_INTERVAL_S", "10")))
    sub.add_parser("status", help="mostrar o estado do spool")
    sub.add_parser("inject-bad", help="acrescentar uma amostra malformada (para testar o gate G1)")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    args = build_parser().parse_args()
    spool = Spool(args.database_url)
    if args.command == "collect":
        try_install(args.database_url)          # caminho live (ADR-022): melhor esforço, nunca impede o arranque
        try:
            asyncio.run(Collector(args.opcua, spool).run())
        except KeyboardInterrupt:
            pass
    elif args.command == "write":
        lake = Lake(args.endpoint, os.environ["LAKEFS_ACCESS_KEY_ID"], os.environ["LAKEFS_SECRET_ACCESS_KEY"], args.repository)
        Writer(spool, lake, load_nodes(), prefix=args.prefix, target=args.target, max_rows=args.batch_rows,
               max_age_s=args.batch_age).run_forever(args.interval)
    elif args.command == "status":
        print(spool.counts())
    elif args.command == "inject-bad":
        now = int(time.time() * 1_000_000)
        spool.add([Event.build("ot-gateway", "ns=2;s=INVENTADO.sensor", "1.0", 0, now, now, now)])
        print("amostra malformada acrescentada: o próximo lote vai ser rejeitado pelo G1")
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
