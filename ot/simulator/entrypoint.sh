#!/bin/sh
# Cria o par série virtual (RS-232) e arranca o simulador no lado /tmp/ttyV1.
# O lado /tmp/ttyV0 fica para o mestre RTU (o gateway da Fase 2).
set -e
socat pty,raw,echo=0,link=/tmp/ttyV0 pty,raw,echo=0,link=/tmp/ttyV1 &
for _ in $(seq 1 50); do
  [ -e /tmp/ttyV0 ] && [ -e /tmp/ttyV1 ] && break
  sleep 0.1
done
exec ot-simulator --serial-port /tmp/ttyV1 "$@"
