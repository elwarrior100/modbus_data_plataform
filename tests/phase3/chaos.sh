#!/usr/bin/env bash
# Teste de caos: mata o contentor do writer várias vezes, com lotes rápidos, e reconcilia no fim.
# Repõe sempre a configuração original, mesmo que falhe a meio.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.lima/industrial/copied-from-guest/kubeconfig.yaml}"
KILLS="${1:-6}"
K="kubectl -n ingest"

restore() { echo "== a repor a configuração normal (lotes de 60 s) =="; $K set env deploy/writer BATCH_AGE_S=60 WRITE_INTERVAL_S=10 >/dev/null; $K rollout status deploy/writer --timeout=120s >/dev/null; }
trap restore EXIT

echo "== lotes rápidos: 5 s de idade, ciclo de 2 s =="
$K set env deploy/writer BATCH_AGE_S=5 WRITE_INTERVAL_S=2 >/dev/null
$K rollout status deploy/writer --timeout=120s >/dev/null
sleep 20

for i in $(seq 1 "$KILLS"); do
  sleep $((8 + RANDOM % 8))
  cid=$(limactl shell industrial -- sudo k3s crictl ps --name writer -q 2>/dev/null | head -1)
  [ -z "$cid" ] && { echo "morte $i: writer ainda a reiniciar, a esperar"; sleep 20; continue; }
  limactl shell industrial -- sudo k3s crictl stop --timeout 0 "$cid" >/dev/null 2>&1
  states=$($K exec deploy/collector -- ingest status 2>/dev/null)
  restarts=$($K get pod -l app=writer -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
  echo "morte $i: restarts do writer=$restarts · logo a seguir ao kill: $states"
  for _ in $(seq 1 40); do   # esperar que o writer volte
    [ "$($K get pod -l app=writer -o jsonpath='{.items[0].status.containerStatuses[0].ready}')" = "true" ] && break
    sleep 5
  done
done

echo "== a deixar o writer acabar o que ficou por fazer =="
sleep 45
restore
trap - EXIT
sleep 30
"$ROOT/tests/phase3/run.sh" 2>&1 | grep -v -E "Terminated|^WARNING"
