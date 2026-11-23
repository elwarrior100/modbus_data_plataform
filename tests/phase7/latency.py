"""Latência ponta a ponta do caminho live (ADR-022, D4; DoD da Fase 7: < 5 s, MEDIDA).

Cada medição: (1) ativa no simulador um `spike` com um valor marcado e único na pressão de saída da bomba
(13,00 + i/100 bar: longe do normal ~5 bar, dentro do intervalo 0–16 e sem regra de alarme); (2) pergunta, de 200 em
200 ms, a mesma consulta do painel "agora" do dashboard live, pela API do Grafana (`/api/ds/query`), até o valor
aparecer; (3) regista o tempo. O relógio é sempre o deste computador (início e fim), por isso não há desvio de relógios.

O que se reporta:
- `dados`: do pedido ao simulador até o Grafana devolver o valor (simulador → gateway → OPC UA → collector → spool →
  trigger → hypertable → Grafana);
- `ecrã` (o que o DoD pede): `dados` + o pior caso de espera pelo próximo refresh do painel (1 s), por isso um limite
  superior, nunca uma média otimista.

Uso: tests/phase7/run.sh (abre os port-forwards e lê a password do Grafana sem a mostrar). Com --via sql pergunta
diretamente ao Postgres (diagnóstico: separa o caminho até à hypertable do Grafana).
"""
import argparse
import base64
import json
import os
import statistics
import time
import urllib.request

ASSET, TAG = "PUMP-01", "outlet_pressure"
PANEL_SQL = (f"SELECT time, value FROM live.readings WHERE asset = '{ASSET}' AND tag = '{TAG}' "
             "AND time > now() - interval '1 minute' ORDER BY time DESC LIMIT 10")
REFRESH_S = 1.0                     # o refresh do painel "agora" (Grafana: min_refresh_interval = 1s)
POLL_S = 0.2


def http(method, url, body=None, headers=None, timeout=5):
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def grafana_values(args) -> list[float]:
    auth = base64.b64encode(f"{os.environ['GRAFANA_USER']}:{os.environ['GRAFANA_PASSWORD']}".encode()).decode()
    body = {"from": "now-1m", "to": "now", "queries": [{
        "refId": "A", "datasource": {"type": "grafana-postgresql-datasource", "uid": "live-timescale"},
        "rawSql": PANEL_SQL, "format": "table", "rawQuery": True}]}
    out = http("POST", f"{args.grafana}/api/ds/query", body, {"Authorization": f"Basic {auth}"})
    frames = out["results"]["A"].get("frames", [])
    if not frames:
        return []
    names = [f["name"] for f in frames[0]["schema"]["fields"]]
    return [v for v in frames[0]["data"]["values"][names.index("value")] if v is not None]


def sql_values(args) -> list[float]:
    import psycopg
    with psycopg.connect(os.environ["LIVE_DSN"], autocommit=True) as db:
        return [r[1] for r in db.execute(PANEL_SQL).fetchall()]


def measure(args, i: int, read) -> float:
    mark = round(13.0 + i / 100, 2)
    t0 = time.monotonic()
    http("POST", f"{args.simulator}/scenarios",
         {"asset": ASSET, "kind": "spike", "duration_s": 3.0, "params": {"signal": TAG, "value": mark}})
    while time.monotonic() - t0 < args.timeout:
        if any(abs(v - mark) < 0.001 for v in read(args)):
            return time.monotonic() - t0
        time.sleep(POLL_S)
    raise TimeoutError(f"o valor marcado {mark} não apareceu em {args.timeout} s")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--grafana", default="http://127.0.0.1:13000")
    p.add_argument("--simulator", default="http://127.0.0.1:18080")
    p.add_argument("--via", choices=("grafana", "sql"), default="grafana")
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--gap", type=float, default=5.0, help="segundos entre medições (o spike dura 3 s)")
    args = p.parse_args()
    read = grafana_values if args.via == "grafana" else sql_values
    data = []
    for i in range(args.runs):
        data.append(measure(args, i, read))
        print(f"{i + 1:2d}/{args.runs}: {data[-1]:.2f} s", flush=True)
        time.sleep(args.gap)
    q = statistics.quantiles(data, n=20, method="inclusive")
    report = {"via": args.via, "runs": len(data), "dados_mediana_s": round(statistics.median(data), 2),
              "dados_p95_s": round(q[18], 2), "dados_max_s": round(max(data), 2)}
    if args.via == "grafana":
        report |= {"ecra_p95_s": round(q[18] + REFRESH_S, 2), "ecra_max_s": round(max(data) + REFRESH_S, 2),
                   "dod_menos_de_5s": q[18] + REFRESH_S < 5.0}
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
