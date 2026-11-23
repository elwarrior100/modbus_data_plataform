#!/usr/bin/env bash
# Mede a latência do caminho live (tests/phase7/latency.py) no cluster. Uso: tests/phase7/run.sh [--via sql] [--runs N]
# Por omissão o cluster local; com KUBECONFIG=~/.kube/oracle.yaml, a Oracle (20 spikes de 3 s na pressão da bomba).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml}"
export GRAFANA_USER="$(kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-user}' | base64 -d)"
export GRAFANA_PASSWORD="$(kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)"
export LIVE_DSN="$(kubectl -n ingest get secret ingest-db -o jsonpath='{.data.DATABASE_URL}' | base64 -d | sed 's#postgres.data.svc.cluster.local:5432#127.0.0.1:15432#')"
kubectl -n monitoring port-forward svc/monitoring-grafana 13000:80 >/dev/null 2>&1 &
PF1=$!
kubectl -n ot port-forward svc/ot-edge 18080:8080 >/dev/null 2>&1 &
PF2=$!
kubectl -n data port-forward svc/postgres 15432:5432 >/dev/null 2>&1 &
PF3=$!
trap 'kill $PF1 $PF2 $PF3 2>/dev/null' EXIT
for _ in $(seq 1 15); do nc -z 127.0.0.1 13000 2>/dev/null && nc -z 127.0.0.1 18080 2>/dev/null && nc -z 127.0.0.1 15432 2>/dev/null && break; sleep 1; done
cd "$ROOT/ingest" && uv run python "$ROOT/tests/phase7/latency.py" "$@"
