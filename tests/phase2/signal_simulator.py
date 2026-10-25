"""Corre dentro do pod: envia um sinal ao processo ot-simulator (SIGSTOP congela, SIGCONT retoma)."""
import os
import signal
import sys

sig = getattr(signal, sys.argv[1])
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        if "ot-simulator" in open(f"/proc/{pid}/cmdline").read():
            os.kill(int(pid), sig)
    except OSError:
        pass
