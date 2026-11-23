"""Silver → gold de um dia UTC, com DuckDB (ADR-019, K3: D1, D3, D4). Só calcula: não toca no lakeFS (isso é o run.py).

A ideia central, em simples: **o dia é uma grelha de 86 400 segundos.** Em cada segundo, cada sinal vale a última
leitura que chegou ("sample and hold"), com um teto:
- analógicos e o `device_clock`: a leitura vale no máximo 60 s (ou o `hold_s` do catálogo: 300 s nos sinais lentos);
  depois disso o segundo fica **sem dados** (o gateway só escreve quando o valor muda, e um analógico a funcionar muda
  sempre, com o ruído; calado por mais do que isso = algo se perdeu);
- sinais de processo (`moves_with_run`): como o estado, valem até à leitura seguinte enquanto há comunicação, porque
  com o ativo parado ficam a 0 e o gateway deixa de os escrever (parado é 0 kW, não "sem dados");
- estado, alarme e contadores: valem até à leitura seguinte **ou até se perder a comunicação** (o `device_clock` do
  ativo, que muda a cada segundo, fica mais de 60 s sem dados, ou chega uma leitura `BAD`);
- uma leitura `BAD` (sem valor) põe o sinal sem dados a partir desse instante.
Assim uma média ponderada pelo tempo é só a média dos segundos com dados, a energia é a soma da potência dos segundos
(kW × s ÷ 3600 = kWh), e os segundos de cada hora somam sempre 3600 (estados + sem dados). Os tempos têm a precisão
de 1 s dos PLC (ADR-018), por isso a grelha ao segundo não perde nada que os dados tenham.

Tamanho: 29 tags × 86 400 s ≈ 2,5 M linhas na grelha, que o DuckDB preenche com uma janela (para cada segundo, a
última leitura até ele). `threads=1` de propósito: as somas de números reais dependem da ordem; com uma só thread a ordem é
sempre a mesma e repetir o dia dá os mesmos bytes.

Hora do evento: `ts_utc`, exceto nas linhas com a flag `clock_skew` (o relógio do equipamento está errado), que usam o
`server_ts` (o do gateway). Lê-se a silver de D−1 23h a D+1 01h (partições pela hora de RECEÇÃO) e fica o que tem a
hora do evento em D. As leituras anteriores a D servem só de "estado inicial" (não contam como amostras de D).
"""
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa

from silver.catalog import Tag, load_catalog
from silver.transform import _lit

from .plan import Plan, load_plan

log = logging.getLogger(__name__)
UTC = timezone.utc
HOLD_S = 60                         # teto do "sample and hold" dos analógicos e do device_clock (ADR-019, contexto)
GOLD_VERSION = "1"                  # versão do código do gold (entra nos metadados do commit e no manifest)

# Cada estado numa categoria comum aos três ativos (o chiller tem outros nomes). "run" é o que conta como a funcionar.
STATE_CATEGORY = {"RUNNING": "run", "COOLING": "run", "STARTING": "starting", "STANDBY": "starting",
                  "STOPPED": "stopped", "OFF": "stopped", "FAULT": "fault", "MAINTENANCE": "maintenance"}
DOWN = ("stopped", "fault", "maintenance")                  # o que é uma paragem em fact_downtime


@dataclass(frozen=True)
class AssetModel:
    """O que o gold sabe de cada ativo, além do catálogo: como se calcula a energia útil (o numerador do rendimento)."""
    asset_type: str
    useful_kind: str                # hydraulic | mechanical | cooling
    useful_sql: str                 # potência útil em kW, a partir dos sinais do segundo
    flow_tag: str | None = None     # o caudal que dá o volume (só a bomba)


ASSETS = {
    # potência hidráulica: caudal (m³/h) × Δp (bar) ÷ 36 = kW
    "PUMP-01": AssetModel("pump", "hydraulic", "flow_rate * (outlet_pressure - inlet_pressure) / 36", "flow_rate"),
    # potência mecânica: binário (N·m) × velocidade (rad/s) ÷ 1000 = kW
    "MOTOR-01": AssetModel("motor", "mechanical", "torque * speed_rpm * 2 * pi() / 60 / 1000"),
    # potência de frio: caudal (m³/h) × 4,19 kJ/(kg·K) × ΔT ÷ 3,6 = kW (1 m³/h de água ≈ 1000 kg / 3600 s)
    "CHILLER-01": AssetModel("chiller", "cooling", "chw_flow * 4.19 * (inlet_temp - outlet_temp) / 3.6"),
}
WIDE_TAGS = ("power_kw", "flow_rate", "outlet_pressure", "inlet_pressure", "torque", "speed_rpm", "chw_flow",
             "inlet_temp", "outlet_temp")              # os sinais de que as contas por ativo precisam, lado a lado

TABLES = ("dim_asset", "dim_tag", "dim_time", "fact_tag_minute", "fact_tag_hour", "fact_asset_hour", "fact_downtime",
          "kpi_asset_shift", "kpi_asset_day")


def gold_path(table: str, day: date) -> str:
    """Todas as tabelas, as `dim_*` incluídas, têm partição por dia (o ADR-019 D4 propunha as `dim_*` sem partição).
    Assim cada ficheiro gold pertence a um dia e à atestação G3 desse dia: o hook confere-o como o do G2 (um caminho →
    uma prova), e um rollback ou um reprocessamento de um dia nunca mexe noutro. As `dim_*` repetem-se (poucos KB):
    ficam como "a dimensão tal como era nesse dia"."""
    return f"gold/{table}/dt={day.isoformat()}/data.parquet"


def silver_window(day: date) -> list[datetime]:
    """As horas de RECEÇÃO a ler para o dia `day`: de D−1 23h a D+1 01h (26 partições)."""
    start = datetime.combine(day, time(0), UTC) - timedelta(hours=1)
    return [start + timedelta(hours=i) for i in range(26)]


@dataclass
class BuildResult:
    day: date
    files: dict[str, Path] = field(default_factory=dict)     # caminho no lake → ficheiro local
    rows: dict[str, int] = field(default_factory=dict)       # tabela → linhas


def _hold(tag: Tag) -> int | None:
    """Quanto tempo vale uma leitura sem outra (None = até à leitura seguinte, se houver comunicação).

    Os sinais de processo (`moves_with_run`: caudal, potência, velocidade…) com o ativo parado ficam a 0 e o gateway deixa
    de os escrever: esse 0 vale até à leitura seguinte, enquanto o `device_clock` do ativo mostrar comunicação. Parado é
    0 kW, não "sem dados" (decisão do Stéphane, 23-11). Os outros analógicos: o `hold_s` do catálogo ou 60 s."""
    if tag.moves_with_run:
        return None
    if tag.kind in ("analog", "clock"):
        return tag.hold_s or HOLD_S
    return None


def _tags_table(catalog: list[Tag]) -> pa.Table:
    clock_id = {t.asset: i for i, t in enumerate(catalog) if t.kind == "clock"}
    return pa.table({
        "tag_id": list(range(len(catalog))),
        "asset_id": [t.asset for t in catalog], "tag_name": [t.tag for t in catalog], "kind": [t.kind for t in catalog],
        "node_id": [t.node_id for t in catalog], "unit": [t.unit for t in catalog],
        "eu_min": pa.array([t.eu_min for t in catalog], pa.float64()),
        "eu_max": pa.array([t.eu_max for t in catalog], pa.float64()),
        "moves_with_run": [t.moves_with_run for t in catalog],
        "hold_s": pa.array([_hold(t) for t in catalog], pa.int64()),
        # estado, alarme e contadores só valem com comunicação (o device_clock do ativo vivo). O `Connected` é do
        # próprio gateway (diz se o dispositivo responde), por isso não depende do relógio do dispositivo.
        # sinais que valem "até à leitura seguinte" só contam enquanto o device_clock do ativo tem dados (comunicação viva)
        "needs_clock": [(t.kind in ("state", "boolean", "counter") and t.tag != "Connected") or t.moves_with_run
                        for t in catalog],
        "clock_tag_id": pa.array([clock_id.get(t.asset) for t in catalog], pa.int64()),
        "numeric": [t.kind in ("analog", "counter", "boolean") for t in catalog],   # média, mín. e máx. fazem sentido
        "enum": pa.array([list(t.enum) for t in catalog], pa.list_(pa.string())),
    })


def _assets_table(catalog: list[Tag]) -> pa.Table:
    assets = sorted({t.asset for t in catalog})
    tags = {(t.asset, t.tag) for t in catalog}
    run_states = {a: sorted({s for t in catalog if t.asset == a and t.kind == "state" for s in t.enum
                             if STATE_CATEGORY.get(s) == "run"}) for a in assets}
    return pa.table({
        "asset_id": assets,
        "asset_type": [ASSETS[a].asset_type for a in assets],
        "run_states": [",".join(run_states[a]) for a in assets],
        "has_alarm": [(a, "alarm") in tags for a in assets],
        "power_tag": ["power_kw" for _ in assets],
        "useful_kind": [ASSETS[a].useful_kind for a in assets],
        "useful_formula_kw": [ASSETS[a].useful_sql for a in assets],
        "flow_tag": [ASSETS[a].flow_tag for a in assets],
    })


def _dim_time(day: date, plan: Plan) -> pa.Table:
    hours = [datetime.combine(day, time(0), UTC) + timedelta(hours=h) for h in range(24)]
    shift = [plan.shift_of(h) for h in hours]
    return pa.table({
        "hour_utc": pa.array(hours, pa.timestamp("us", tz="UTC")),
        "utc_date": pa.array([day] * 24, pa.date32()),
        "lisbon_hour": pa.array([h.astimezone(plan.tz).replace(tzinfo=None) for h in hours], pa.timestamp("us")),
        "lisbon_date": pa.array([h.astimezone(plan.tz).date() for h in hours], pa.date32()),
        "production_date": pa.array([s[0] for s in shift], pa.date32()),
        "shift": [s[1] for s in shift],
    })


def _periods(day: date, plan: Plan) -> pa.Table:
    ps = plan.periods_ending_in(day)
    return pa.table({
        "production_date": pa.array([p.production_date for p in ps], pa.date32()),
        "shift": pa.array([p.shift for p in ps], pa.string()),
        "start_utc": pa.array([p.start for p in ps], pa.timestamp("us", tz="UTC")),
        "end_utc": pa.array([p.end for p in ps], pa.timestamp("us", tz="UTC")),
        "expected_hours": pa.array([p.hours for p in ps], pa.int64()),
    })


def _windows(day: date, plan: Plan) -> pa.Table:
    lo = datetime.combine(day, time(0), UTC)
    ws = plan.stop_windows(lo, lo + timedelta(days=1))
    return pa.table({"asset_id": pa.array([w[0] for w in ws], pa.string()), "kind": pa.array([w[1] for w in ws], pa.string()),
                     "b": pa.array([int(w[2].timestamp()) for w in ws], pa.int64()),
                     "e": pa.array([int(w[3].timestamp()) for w in ws], pa.int64())})


def _category_sql(col: str) -> str:
    whens = " ".join(f"WHEN '{s}' THEN '{c}'" for s, c in STATE_CATEGORY.items())
    return f"CASE WHEN {col} IS NULL THEN 'no_data' ELSE CASE {col} {whens} ELSE 'no_data' END END"


def _per_asset(expr: dict[str, str | None]) -> str:
    whens = " ".join(f"WHEN '{a}' THEN {e}" for a, e in expr.items() if e)
    return f"CASE asset_id {whens} END" if whens else "NULL"


def build_day(silver_files: list[str | Path], day: date, out_dir: str | Path, *, catalog: list[Tag] | None = None,
              plan: Plan | None = None, previous_tag_hour: str | Path | None = None,
              previous_asset_hour: str | Path | None = None,
              memory_limit: str = "256MB", threads: int = 1) -> BuildResult:
    """Calcula as tabelas gold do dia UTC `day` a partir dos ficheiros silver da janela (`silver_window`).
    `previous_tag_hour` e `previous_asset_hour` são o `fact_tag_hour` e o `fact_asset_hour` do gold de D−1, se existirem:
    o primeiro dá o estado inicial dos sinais que só se escrevem ao mudar (o estado de uma máquina pode não mudar durante
    dias), o segundo as horas de D−1 dos turnos e dias que acabam em D. Sem eles, esses sinais começam "sem dados" até à
    primeira leitura e esses KPIs ficam `complete = false`. Por isso os dias calculam-se por ordem (o DAG faz isso)."""
    catalog = catalog or load_catalog()
    plan = plan or load_plan()
    out_dir = Path(out_dir)
    t0 = int(datetime.combine(day, time(0), UTC).timestamp())
    t1 = t0 + 86400
    con = duckdb.connect(config={"memory_limit": memory_limit, "threads": threads})
    con.execute("SET TimeZone = 'UTC'")
    con.execute("SET preserve_insertion_order = true")
    con.register("tags", _tags_table(catalog))
    con.register("dim_asset_src", _assets_table(catalog))
    con.register("windows", _windows(day, plan))
    con.register("periods", _periods(day, plan))
    con.register("dim_time_src", _dim_time(day, plan))
    clock = [_time.monotonic()]

    def q(sql: str):
        """Executa um passo; com o log em DEBUG mostra quanto tempo levou (para medir o gold, ADR-019 §3)."""
        out = con.execute(sql)
        log.debug("%6.2f s  %s", _time.monotonic() - clock[0], " ".join(sql.split())[:80])
        clock[0] = _time.monotonic()
        return out

    # 1. As leituras da janela, com o instante do evento (t_us) e o segundo inteiro (sec) -------------------------------
    # Colunas pequenas de propósito (medido: ~1,3 M de linhas por dia): o bronze_event_id só serve para desempatar duas
    # leituras com o mesmo instante e o mesmo server_ts, por isso guarda-se o seu hash (8 bytes em vez de 36 caracteres).
    q("""CREATE TABLE raw (tag_id BIGINT, t_us BIGINT, sec BIGINT, good BOOLEAN, ok BOOLEAN, v DOUBLE, label VARCHAR,
                           srv_us BIGINT, eid UBIGINT)""")
    if silver_files:
        files = "[" + ", ".join(_lit(f) for f in silver_files) + "]"
        q(f"""INSERT INTO raw
              WITH s AS (SELECT *, epoch_us(CASE WHEN list_contains(dq_flags, 'clock_skew') THEN server_ts ELSE ts_utc END) AS t_us
                         FROM read_parquet({files}, union_by_name = true, hive_partitioning = false))
              SELECT g.tag_id, s.t_us, s.t_us // 1000000, s.quality = 'GOOD', s.quality <> 'BAD',
                     CASE g.kind WHEN 'boolean' THEN CAST(s.value_bool AS DOUBLE) WHEN 'clock' THEN NULL ELSE s.value_double END,
                     CASE g.kind WHEN 'state' THEN s.value_str END, epoch_us(s.server_ts), hash(s.bronze_event_id)
              FROM s JOIN tags g ON g.asset_id = s.asset_id AND g.tag_name = s.tag_name
              WHERE s.t_us // 1000000 < {t1}""")
    # a ordem total das leituras (o "primeiro" e o "último" nunca empatam): instante, server_ts (ADR-018), id
    order = "{'t': t_us, 's': srv_us, 'e': eid}"

    # 2. Uma leitura por (tag, segundo): a última desse segundo -------------------------------------------------------
    q("""CREATE TABLE ps AS SELECT tag_id, sec, ok, v, label FROM raw
         QUALIFY row_number() OVER (PARTITION BY tag_id, sec ORDER BY t_us DESC, srv_us DESC NULLS LAST, eid DESC) = 1""")
    # Estado inicial: os sinais que valem "até à leitura seguinte" (estado, alarme, contadores, Connected) podem não ter
    # nenhuma leitura há dias (o gateway só escreve ao mudar). Se a janela não tiver nenhuma leitura deles antes de D,
    # entra o valor em vigor no fim de D−1 (`value_end` da hora 23 do fact_tag_hour de D−1), como uma leitura no último
    # segundo de D−1. Os analógicos não precisam: valem no máximo 60 s, e esses estão na partição D−1 23h.
    if previous_tag_hour is not None:
        q(f"""INSERT INTO ps
              SELECT g.tag_id, {t0 - 1}, true, p.value_end,
                     CASE WHEN g.kind = 'state' THEN g.enum[CAST(p.value_end AS INTEGER) + 1] END
              FROM read_parquet({_lit(previous_tag_hour)}, hive_partitioning = false) p
              JOIN tags g ON g.asset_id = p.asset_id AND g.tag_name = p.tag_name
              WHERE epoch_us(p.hour_utc) // 1000000 = {t0 - 3600} AND g.hold_s IS NULL AND p.value_end IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM ps x WHERE x.tag_id = g.tag_id AND x.sec < {t0})""")

    # 3. A grelha: para cada tag e segundo de D, a última leitura até esse segundo, e se ainda vale -------------------
    # "a última leitura até ao segundo s" = last_value(... IGNORE NULLS) numa janela ordenada pelo segundo. A leitura vai
    # inteira num struct (segundo, ok, valor): uma leitura BAD tem valor nulo, e não se pode saltar para o valor de uma
    # leitura mais antiga. As leituras anteriores a D entram como linhas extra (só para dar o estado inicial).
    # Um ativo de cada vez: a memória do passo mais pesado fica ~3× menor (medido; ADR-019 §3).
    # (Um ASOF JOIN dava o mesmo, mas com threads=1 levava minutos; a janela leva < 1 s.)
    q("CREATE TABLE held (tag_id BIGINT, sec BIGINT, has BOOLEAN, v DOUBLE)")
    for asset in sorted({t.asset for t in catalog}):
        mine = f"(SELECT tag_id FROM tags WHERE asset_id = {_lit(asset)})"
        q(f"""CREATE OR REPLACE TEMP TABLE held0 AS
              WITH u AS (
                SELECT g.tag_id, g.sec, true AS is_grid,
                       CASE WHEN p.sec IS NOT NULL THEN {{'sec': p.sec, 'ok': p.ok, 'v': p.v}} END AS r
                FROM (SELECT t.tag_id, x.range AS sec FROM tags t, range({t0}, {t1}) x WHERE t.tag_id IN {mine}) g
                LEFT JOIN ps p ON p.tag_id = g.tag_id AND p.sec = g.sec
                UNION ALL
                SELECT tag_id, sec, false, {{'sec': sec, 'ok': ok, 'v': v}} FROM ps WHERE sec < {t0} AND tag_id IN {mine}),
              l AS (SELECT tag_id, sec, is_grid,
                           last_value(r IGNORE NULLS) OVER (PARTITION BY tag_id ORDER BY sec ROWS UNBOUNDED PRECEDING) AS r
                    FROM u)
              SELECT l.tag_id, l.sec, coalesce(l.r.ok AND (t.hold_s IS NULL OR l.sec - l.r.sec < t.hold_s), false) AS has,
                     l.r.v AS v
              FROM l JOIN tags t USING (tag_id) WHERE l.is_grid""")
        # estado, alarme e contadores: só valem se o device_clock do ativo também tem dados nesse segundo (comunicação viva)
        q("""INSERT INTO held
             SELECT tag_id, sec, has, CASE WHEN has THEN v END
             FROM (SELECT h.tag_id, h.sec, h.has AND (NOT t.needs_clock OR coalesce(c.has, false)) AS has, h.v
                   FROM held0 h JOIN tags t USING (tag_id)
                   LEFT JOIN (SELECT sec, has FROM held0 WHERE tag_id IN (SELECT tag_id FROM tags WHERE kind = 'clock')) c
                     ON c.sec = h.sec)
             ORDER BY tag_id, sec""")
    q("DROP TABLE held0")

    # 4. Por ativo e segundo: o estado e os sinais das contas, lado a lado --------------------------------------------
    wide = ",\n".join(f"max(h.v) FILTER (WHERE t.tag_name = '{n}') AS {n}" for n in WIDE_TAGS)
    q(f"""CREATE TABLE asec0 AS
          SELECT t.asset_id, h.sec, max(t.enum[CAST(h.v AS INTEGER) + 1]) FILTER (WHERE t.tag_name = 'state') AS state,
                 max(h.v) FILTER (WHERE t.tag_name = 'alarm') AS alarm, {wide}
          FROM held h JOIN tags t USING (tag_id)
          WHERE t.tag_name IN ('state', 'alarm', {", ".join(f"'{n}'" for n in WIDE_TAGS)})
          GROUP BY ALL""")
    useful = _per_asset({a: m.useful_sql for a, m in ASSETS.items()})
    flow = _per_asset({a: m.flow_tag for a, m in ASSETS.items()})
    q(f"""CREATE TABLE asec AS
          SELECT asset_id, sec, state, {_category_sql('state')} AS cat, alarm, power_kw, {flow} AS flow, {useful} AS useful_kw,
                 EXISTS (SELECT 1 FROM windows w WHERE w.asset_id = a.asset_id AND a.sec >= w.b AND a.sec < w.e) AS planned
          FROM asec0 a""")
    q("DROP TABLE asec0")

    # 5. Falhas (entradas em FAULT) e alarmes (falso → verdadeiro), das leituras (com o estado inicial) -----------------
    q(f"""CREATE TABLE events AS
          WITH s AS (SELECT t.asset_id, t.tag_name, p.sec, p.v, p.label,
                            lag(p.label) OVER w AS prev_label, lag(p.v) OVER w AS prev_v
                     FROM ps p JOIN tags t USING (tag_id)
                     WHERE t.tag_name IN ('state', 'alarm') AND p.ok
                     WINDOW w AS (PARTITION BY p.tag_id ORDER BY p.sec))
          SELECT asset_id, sec // 3600 * 3600 AS hb,
                 count(*) FILTER (WHERE tag_name = 'state' AND label = 'FAULT' AND prev_label IS DISTINCT FROM 'FAULT') AS failures,
                 count(*) FILTER (WHERE tag_name = 'alarm' AND v = 1 AND coalesce(prev_v, 0) = 0) AS alarms
          FROM s WHERE sec >= {t0} AND sec < {t1} GROUP BY ALL""")

    # 6. fact_asset_hour: só medidas que se somam (segundos, kWh, m³, contagens) ---------------------------------------
    q("""CREATE TABLE fact_asset_hour AS
         WITH h AS (
           SELECT asset_id, sec // 3600 * 3600 AS hb,
             count(*) FILTER (WHERE cat = 'run') AS run_s, count(*) FILTER (WHERE cat = 'starting') AS starting_s,
             count(*) FILTER (WHERE cat = 'stopped') AS stopped_s, count(*) FILTER (WHERE cat = 'fault') AS fault_s,
             count(*) FILTER (WHERE cat = 'maintenance') AS maintenance_s, count(*) FILTER (WHERE cat = 'no_data') AS no_data_s,
             count(*) FILTER (WHERE planned) AS planned_stop_s,
             count(*) FILTER (WHERE NOT planned) AS planned_s,
             count(*) FILTER (WHERE NOT planned AND cat <> 'no_data') AS planned_with_data_s,
             count(*) FILTER (WHERE NOT planned AND cat = 'run') AS run_planned_s,
             coalesce(sum(power_kw), 0) / 3600 AS energy_kwh, count(power_kw) AS energy_s,
             sum(flow) / 3600 AS volume_m3, count(flow) AS volume_s,
             coalesce(sum(useful_kw) FILTER (WHERE power_kw IS NOT NULL), 0) / 3600 AS useful_kwh,
             coalesce(sum(power_kw) FILTER (WHERE useful_kw IS NOT NULL), 0) / 3600 AS useful_basis_kwh
           FROM asec GROUP BY ALL)
         SELECT h.asset_id, to_timestamp(h.hb) AS hour_utc,
                run_s, starting_s, stopped_s, fault_s, maintenance_s, no_data_s,
                planned_stop_s, planned_s, planned_with_data_s, run_planned_s,
                coalesce(e.failures, 0) AS failures,
                CASE WHEN d.has_alarm THEN coalesce(e.alarms, 0) END AS alarms,
                round(energy_kwh, 6) AS energy_kwh, energy_s,
                CASE WHEN d.flow_tag IS NOT NULL THEN round(coalesce(volume_m3, 0), 6) END AS volume_m3,
                CASE WHEN d.flow_tag IS NOT NULL THEN volume_s END AS volume_s,
                round(useful_kwh, 6) AS useful_kwh, round(useful_basis_kwh, 6) AS useful_basis_kwh
         FROM h JOIN dim_asset_src d USING (asset_id) LEFT JOIN events e USING (asset_id, hb)
         ORDER BY asset_id, hour_utc""")

    # 7. fact_tag_minute e fact_tag_hour: amostras (as leituras do dia) + valores ponderados pelo tempo (a grelha) -----
    def tag_grain(seconds: int, column: str) -> str:
        minutes = (", count(DISTINCT sec // 60) FILTER (WHERE good) AS minutes_with_data"
                   if seconds > 60 else "")
        minutes_out = ", coalesce(s.minutes_with_data, 0) AS minutes_with_data" if seconds > 60 else ""
        return f"""
          WITH s AS (
            SELECT tag_id, sec // {seconds} * {seconds} AS b, count(*) AS samples,
                   count(*) FILTER (WHERE good) AS good_samples {minutes},
                   min(v) FILTER (WHERE v IS NOT NULL) AS min_value, max(v) FILTER (WHERE v IS NOT NULL) AS max_value,
                   arg_min(v, {order}) FILTER (WHERE v IS NOT NULL) AS first_value,
                   arg_max(v, {order}) FILTER (WHERE v IS NOT NULL) AS last_value
            FROM raw WHERE sec >= {t0} GROUP BY ALL),   -- só as leituras do dia contam como amostras
          h AS (SELECT tag_id, sec // {seconds} * {seconds} AS b, count(*) FILTER (WHERE has) AS seconds_with_data,
                       avg(v) FILTER (WHERE has) AS mean_value,
                       max(v) FILTER (WHERE sec % {seconds} = {seconds - 1}) AS value_end
                FROM held GROUP BY ALL)
          SELECT t.asset_id, t.tag_name, to_timestamp(h.b) AS {column},
                 coalesce(s.samples, 0) AS samples, coalesce(s.good_samples, 0) AS good_samples,
                 CASE WHEN s.samples > 0 THEN round(s.good_samples / s.samples, 6) END AS good_ratio {minutes_out},
                 h.seconds_with_data,
                 CASE WHEN t.numeric THEN s.min_value END AS min_value, CASE WHEN t.numeric THEN s.max_value END AS max_value,
                 CASE WHEN t.numeric THEN round(h.mean_value, 6) END AS mean_value,
                 CASE WHEN t.kind <> 'clock' THEN s.first_value END AS first_value,
                 CASE WHEN t.kind <> 'clock' THEN s.last_value END AS last_value,
                 h.value_end
          FROM h JOIN tags t USING (tag_id) LEFT JOIN s USING (tag_id, b)
          ORDER BY t.asset_id, t.tag_name, h.b"""
    q("CREATE TABLE fact_tag_minute AS " + tag_grain(60, "minute_utc"))
    q("CREATE TABLE fact_tag_hour AS " + tag_grain(3600, "hour_utc"))

    # 8. fact_downtime: cada intervalo seguido com o mesmo estado de paragem (ilhas: sec − n.º de ordem é constante) --
    q(f"""CREATE TABLE fact_downtime AS
          WITH d AS (SELECT asset_id, sec, cat, state, planned FROM asec WHERE cat IN ({", ".join(f"'{c}'" for c in DOWN)})),
          g AS (SELECT *, sec - row_number() OVER (PARTITION BY asset_id, cat, state ORDER BY sec) AS grp FROM d)
          SELECT asset_id, to_timestamp(min(sec)) AS start_utc, to_timestamp(max(sec) + 1) AS end_utc,
                 count(*) AS duration_s, cat AS cause, state, count(*) FILTER (WHERE planned) AS planned_s,
                 min(sec) = {t0} AS from_previous_day, max(sec) + 1 = {t1} AS into_next_day
          FROM g GROUP BY asset_id, cat, state, grp
          ORDER BY asset_id, start_utc""")

    # 9. KPIs de turno e de dia de produção, calculados das SOMAS horárias (nunca média de médias) --------------------
    q("CREATE TABLE fah_all AS SELECT * FROM fact_asset_hour")
    if previous_asset_hour is not None:
        q(f"INSERT INTO fah_all BY NAME SELECT * FROM read_parquet({_lit(previous_asset_hour)}, hive_partitioning = false)")
    q("""CREATE TABLE kpi AS
         WITH s AS (
           SELECT p.production_date, p.shift, p.start_utc, p.end_utc, p.expected_hours, f.asset_id,
                  count(*) AS hours, sum(run_s)::BIGINT AS run_s, sum(fault_s)::BIGINT AS fault_s,
                  sum(no_data_s)::BIGINT AS no_data_s, sum(planned_stop_s)::BIGINT AS planned_stop_s,
                  sum(planned_with_data_s)::BIGINT AS planned_with_data_s, sum(run_planned_s)::BIGINT AS run_planned_s,
                  sum(failures)::BIGINT AS failures, sum(alarms)::BIGINT AS alarms,
                  round(sum(energy_kwh), 6) AS energy_kwh, sum(energy_s)::BIGINT AS energy_s,
                  round(sum(volume_m3), 6) AS volume_m3,
                  round(sum(useful_kwh), 6) AS useful_kwh, round(sum(useful_basis_kwh), 6) AS useful_basis_kwh
           FROM periods p JOIN fah_all f ON f.hour_utc >= p.start_utc AND f.hour_utc < p.end_utc
           GROUP BY ALL)
         SELECT s.*, s.hours = s.expected_hours AS complete,
                round(1 - no_data_s / (hours * 3600.0), 6) AS coverage,
                round(run_planned_s / nullif(planned_with_data_s, 0), 6) AS availability,
                CASE WHEN failures > 0 THEN round(run_s / 3600.0 / failures, 6) END AS mtbf_h,
                CASE WHEN failures > 0 THEN round(fault_s / 3600.0 / failures, 6) END AS mttr_h,
                CASE WHEN run_s > 0 THEN round(alarms * 86400.0 / run_s, 6) END AS alarms_per_24h_run,
                -- sem nenhum segundo de potência (ex.: silver anterior aos sinais novos, ADR-019 D11) não há kWh/m³;
                -- a potência e o caudal da bomba vêm do mesmo PLC, por isso faltam juntos numa falha de comunicação
                CASE WHEN volume_m3 > 0 AND energy_s > 0 THEN round(energy_kwh / volume_m3, 6) END AS kwh_per_m3,
                CASE WHEN d.asset_type IN ('pump', 'motor') AND useful_basis_kwh > 0 THEN round(useful_kwh / useful_basis_kwh, 6) END AS efficiency,
                CASE WHEN d.asset_type = 'chiller' AND useful_basis_kwh > 0 THEN round(useful_kwh / useful_basis_kwh, 6) END AS cop
         FROM s JOIN dim_asset_src d USING (asset_id)""")

    # 10. Escrever: linhas ordenadas e nome fixo → repetir dá o mesmo ficheiro ----------------------------------------
    selects = {
        "dim_asset": "SELECT * FROM dim_asset_src ORDER BY asset_id",
        "dim_tag": "SELECT asset_id, tag_name, node_id, kind, unit, eu_min, eu_max, moves_with_run, hold_s FROM tags ORDER BY asset_id, tag_name",
        "dim_time": "SELECT * FROM dim_time_src ORDER BY hour_utc",
        "fact_tag_minute": "SELECT * FROM fact_tag_minute",
        "fact_tag_hour": "SELECT * FROM fact_tag_hour",
        "fact_asset_hour": "SELECT * FROM fact_asset_hour",
        "fact_downtime": "SELECT * FROM fact_downtime",
        "kpi_asset_shift": "SELECT * EXCLUDE (expected_hours) FROM kpi WHERE shift IS NOT NULL ORDER BY end_utc, asset_id",
        "kpi_asset_day": "SELECT * EXCLUDE (shift, expected_hours) FROM kpi WHERE shift IS NULL ORDER BY end_utc, asset_id",
    }
    result = BuildResult(day)
    for table, sql in selects.items():
        local = out_dir / gold_path(table, day)
        local.parent.mkdir(parents=True, exist_ok=True)
        q(f"COPY ({sql}) TO {_lit(local)} (FORMAT PARQUET, COMPRESSION ZSTD)")
        result.files[gold_path(table, day)] = local
        result.rows[table] = q(f"SELECT count(*) FROM ({sql})").fetchone()[0]
    con.close()
    return result
