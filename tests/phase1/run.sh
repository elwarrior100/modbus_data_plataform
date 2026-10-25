#!/usr/bin/env bash
# Verifica o simulador OT no cluster: Modbus TCP (do Mac) e RS-232 virtual (dentro do pod).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml}"

kubectl -n ot port-forward svc/ot-simulator 15020:502 18080:8080 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
for _ in $(seq 1 15); do nc -z 127.0.0.1 15020 2>/dev/null && nc -z 127.0.0.1 18080 2>/dev/null && break; sleep 1; done

echo "== Modbus TCP =="
(cd "$ROOT/ot/simulator" && uv run python "$ROOT/tests/phase1/verify_tcp.py" 127.0.0.1 15020 18080)
echo "== RS-232 virtual (dentro do pod) =="
kubectl -n ot exec -i deploy/ot-simulator -- python - < "$ROOT/tests/phase1/verify_serial.py"
echo "=== FASE 1 OK ==="
