#!/usr/bin/env bash
# Corre o teste de fumo lakeFS + DuckDB contra o cluster. Idempotente.
# Requer: kubectl, lakectl, duckdb, e a VM Lima "industrial" a correr.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml}"
PORT=18000
REPO=lakefs://industrial-lake

secret() { kubectl -n data get secret lakefs-auth -o "jsonpath={.data.$1}" | base64 -d; }
KEY_ID="$(secret ADMIN_ACCESS_KEY_ID)"
KEY_SECRET="$(secret ADMIN_SECRET_ACCESS_KEY)"

kubectl -n data port-forward svc/lakefs "$PORT:8000" >/dev/null 2>&1 &
PF=$!
TMP="$(mktemp)"
trap 'kill $PF 2>/dev/null; rm -f "$TMP"' EXIT
for _ in $(seq 1 15); do nc -z 127.0.0.1 "$PORT" 2>/dev/null && break; sleep 1; done

export LAKECTL_SERVER_ENDPOINT_URL="http://127.0.0.1:$PORT"
export LAKECTL_CREDENTIALS_ACCESS_KEY_ID="$KEY_ID"
export LAKECTL_CREDENTIALS_SECRET_ACCESS_KEY="$KEY_SECRET"

# Refs de teste: criadas só se ainda não existirem.
lakectl tag list "$REPO" 2>/dev/null | grep -q gold-v0.1.0 || lakectl tag create "$REPO/gold-v0.1.0" "$REPO/main" >/dev/null
if ! lakectl branch list "$REPO" 2>/dev/null | grep -q '^etl-gap'; then
  lakectl branch create "$REPO/etl-gap" --source "$REPO/main" >/dev/null
  lakectl fs rm "$REPO/etl-gap/bronze/dt=2026-03-02/data_0.parquet" >/dev/null
  lakectl commit "$REPO/etl-gap" -m "simula comms_gap: perde-se 2026-03-02" >/dev/null
fi

umask 077
{
  echo "INSTALL httpfs; LOAD httpfs;"
  echo "CREATE SECRET lake (TYPE S3, KEY_ID '$KEY_ID', SECRET '$KEY_SECRET', ENDPOINT '127.0.0.1:$PORT', URL_STYLE 'path', USE_SSL false, REGION 'us-east-1');"
  cat "$DIR/lakefs_duckdb.sql"
} > "$TMP"

set +e
duckdb -batch < "$TMP"
RC=$?
set -e
if [ "$RC" -eq 0 ]; then echo "=== SPIKE OK ==="; else echo "=== SPIKE FALHOU (exit $RC) ==="; fi
exit "$RC"
