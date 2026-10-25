#!/bin/bash
# Arranca o par série virtual, o simulador (lado ttyV1) e o gateway (lado ttyV0).
# Se qualquer processo terminar, o contentor termina e o Kubernetes reinicia-o.
set -e
socat pty,raw,echo=0,link=/tmp/ttyV0 pty,raw,echo=0,link=/tmp/ttyV1 &
for _ in $(seq 1 50); do
  [ -e /tmp/ttyV0 ] && [ -e /tmp/ttyV1 ] && break
  sleep 0.1
done
trap 'kill $(jobs -p) 2>/dev/null' EXIT

ot-simulator --serial-port /tmp/ttyV1 ${SCENARIOS_FILE:+--scenarios "$SCENARIOS_FILE"} &
ot-gateway --modbus "rtu:///tmp/ttyV0?baud=19200&parity=N" &

wait -n
echo "um dos processos terminou; a sair" >&2
exit 1
