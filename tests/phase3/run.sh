#!/usr/bin/env bash
# Reconcilia o spool com o lakeFS no cluster. Uso: tests/phase3/run.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml}"
export LAKEFS_ENDPOINT=http://127.0.0.1:18000
export LAKEFS_ACCESS_KEY_ID="$(kubectl -n data get secret lakefs-auth -o jsonpath='{.data.ADMIN_ACCESS_KEY_ID}' | base64 -d)"
export LAKEFS_SECRET_ACCESS_KEY="$(kubectl -n data get secret lakefs-auth -o jsonpath='{.data.ADMIN_SECRET_ACCESS_KEY}' | base64 -d)"

export INGEST_DSN="$(kubectl -n ingest get secret ingest-db -o jsonpath='{.data.DATABASE_URL}' | base64 -d | sed 's#postgres.data.svc.cluster.local:5432#127.0.0.1:15432#')"

kubectl -n data port-forward svc/lakefs 18000:8000 >/dev/null 2>&1 &
PF1=$!
kubectl -n data port-forward svc/postgres 15432:5432 >/dev/null 2>&1 &
PF2=$!
trap 'kill $PF1 $PF2 2>/dev/null' EXIT
for _ in $(seq 1 15); do nc -z 127.0.0.1 18000 2>/dev/null && nc -z 127.0.0.1 15432 2>/dev/null && break; sleep 1; done
cd "$ROOT/ingest" && uv run python "$ROOT/tests/phase3/reconcile.py"
