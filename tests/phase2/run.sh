#!/usr/bin/env bash
# Verifica o gateway OPC UA no cluster, com um cliente independente a correr no Mac.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml}"
SIGNAL="kubectl -n ot exec -i deploy/ot-edge -- python - "

kubectl -n ot port-forward svc/ot-edge 14840:4840 18080:8080 >/dev/null 2>&1 &
PF=$!
# Mesmo que o teste falhe a meio, o simulador nunca fica congelado.
trap 'kill $PF 2>/dev/null; $SIGNAL SIGCONT < "$ROOT/tests/phase2/signal_simulator.py" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 15); do nc -z 127.0.0.1 14840 2>/dev/null && nc -z 127.0.0.1 18080 2>/dev/null && break; sleep 1; done

FREEZE="$SIGNAL SIGSTOP < $ROOT/tests/phase2/signal_simulator.py"
RESUME="$SIGNAL SIGCONT < $ROOT/tests/phase2/signal_simulator.py"
cd "$ROOT/ot/gateway"
uv run python "$ROOT/tests/phase2/verify_opcua.py" 14840 18080 "$FREEZE" "$RESUME"
echo "=== FASE 2 OK ==="
