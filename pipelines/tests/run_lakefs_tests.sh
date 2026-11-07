#!/usr/bin/env bash
# Corre os testes que usam o lakeFS real do cluster. As chaves entram no ambiente DESTE processo
# (nada é impresso nem gravado). Precisa do túnel aberto: ./platform/scripts/ui.sh (porto 8082).
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG="${KUBECONFIG:-$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml}"
export LAKEFS_ENDPOINT="${LAKEFS_ENDPOINT:-http://localhost:8082}"
LAKEFS_ACCESS_KEY_ID="$(kubectl -n data get secret lakefs-auth -o jsonpath='{.data.ADMIN_ACCESS_KEY_ID}' | base64 -d)"
LAKEFS_SECRET_ACCESS_KEY="$(kubectl -n data get secret lakefs-auth -o jsonpath='{.data.ADMIN_SECRET_ACCESS_KEY}' | base64 -d)"
export LAKEFS_ACCESS_KEY_ID LAKEFS_SECRET_ACCESS_KEY
exec uv run pytest -m lakefs -v "$@"
