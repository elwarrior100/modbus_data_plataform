"""Gate G3: coerência do gold de um dia (docs/gates-contract.md, secção 6; ADR-019, D6).

Como o G2: lê os ficheiros que acabámos de gerar e devolve um relatório. Não corrige nada; só decide `pass`, `warn` ou
`fail` (alguma regra `blocker` falhou). Quem impõe é o hook do lakeFS (`quality/hooks/g3_pre_merge.lua`), que confere a
atestação `_gates/g3/dt=YYYY-MM-DD.json` com o checksum de cada ficheiro gold do dia.

O G3 lê a silver **por conta própria** (não confia nas contas do gold): a reconciliação só prova alguma coisa se as duas
contagens forem feitas por caminhos diferentes.
"""
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import duckdb

from silver.g2 import Finding
from silver.transform import _lit

from .build import GOLD_VERSION, gold_path

UTC = timezone.utc
# limiares (a calibrar, como os do G2)
MISSING_HOURS_BLOCK = 6              # G3-04: mais do que isto de horas sem silver no main → bloqueio
KWH_PER_M3 = (0.05, 2.0)             # G3-03: consumo específico plausível para uma bomba de água
COP_MAX = 12.0                       # G3-03: um chiller real fica muito abaixo do limite de Carnot (~15 nestas temperaturas)
VOLUME_TOLERANCE = 0.20              # G3-05: ±20 % das amostras contra a média dos 7 dias anteriores
PSI_WARN = 0.25                      # G3-06: PSI acima disto = a distribuição mudou
MIN_HISTORY_DAYS = 3                 # G3-06: com menos dias anteriores no gold, o PSI não diz nada (fica `info`)


@dataclass
class Report:
    partition: str                   # o dia, YYYY-MM-DD
    rows: int                        # amostras da silver no dia (as que o gold resume)
    findings: list[Finding] = field(default_factory=list)

    @property
    def status(self) -> str:
        severities = {f.severity for f in self.findings}
        return "fail" if "blocker" in severities else "warn" if "warn" in severities else "pass"

    @property
    def summary(self) -> str:
        shown = [f for f in self.findings if f.severity != "info"][:4]
        if not shown:
            return "sem achados"
        return "; ".join(f"{f.id}{'/' + f.asset if f.asset else ''} {f.severity}: {f.detail}" for f in shown)

    def attestation(self, files: dict[str, str], inputs: dict | None = None) -> bytes:
        """O ficheiro `_gates/g3/dt=….json`. `files`: caminho gold → checksum do lakeFS (o que o hook confere).
        `inputs`: de onde veio o gold (commit da silver, partições, commit de bronze de cada uma); o manifest da release
        copia-o daqui, para a release levar a prova consigo (ADR-019, D7 e D8)."""
        doc = {"gate": "G3", "version": 1, "status": self.status, "partition": self.partition, "files": files,
               "rows": self.rows, "rules": [asdict(f) for f in self.findings], "summary": self.summary,
               "gold_version": GOLD_VERSION, "inputs": inputs or {}}
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()


def _src(path) -> str:
    return f"read_parquet({_lit(path)}, hive_partitioning = false)"


def psi(reference: list[float], current: list[float], bins: int = 10) -> float | None:
    """Population Stability Index: quanto a distribuição de `current` se afastou da de `reference`.
    Intervalos pelos decis da referência; < 0,1 estável, 0,1–0,25 alguma mudança, > 0,25 mudou. None se não há dados."""
    reference, current = sorted(x for x in reference if x is not None), [x for x in current if x is not None]
    if len(reference) < bins or not current:
        return None
    edges = sorted({reference[int(len(reference) * i / bins)] for i in range(1, bins)})

    def shares(values):
        counts = [0] * (len(edges) + 1)
        for v in values:
            counts[sum(v >= e for e in edges)] += 1
        return [(c + 0.5) / (len(values) + 0.5 * len(counts)) for c in counts]     # +0,5: nenhum intervalo a zero

    ref, cur = shares(reference), shares(current)
    return sum((c - r) * math.log(c / r) for r, c in zip(ref, cur))


def evaluate(day: date, gold_files: dict[str, Path], silver_files: list[Path], *, silver_hours: set[int],
             history: list[Path] | None = None) -> Report:
    """`gold_files`: caminho gold → ficheiro local (o resultado do build). `silver_files`: os ficheiros da janela.
    `silver_hours`: as horas (0–23) do dia D que têm silver no `main` (as outras foram recusadas pelo G2 ou não correram).
    `history`: os `fact_tag_hour` dos dias anteriores que existem no gold (até 7), para o volume e a deriva."""
    history = history or []
    t0 = datetime.combine(day, time(0), UTC)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    for table in ("fact_tag_hour", "fact_tag_minute", "fact_asset_hour", "kpi_asset_shift", "kpi_asset_day"):
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM {_src(gold_files[gold_path(table, day)])}")
    one = lambda sql: con.execute(sql).fetchall()[0][0]     # noqa: E731

    # a silver do dia, contada à parte: a mesma regra do instante do evento (clock_skew → server_ts)
    if silver_files:
        files = "[" + ", ".join(_lit(f) for f in silver_files) + "]"
        con.execute(f"""CREATE VIEW silver_day AS
            SELECT asset_id, tag_name FROM read_parquet({files}, hive_partitioning = false)
            WHERE CASE WHEN list_contains(dq_flags, 'clock_skew') THEN server_ts ELSE ts_utc END
                  >= TIMESTAMPTZ '{t0.isoformat()}'
              AND CASE WHEN list_contains(dq_flags, 'clock_skew') THEN server_ts ELSE ts_utc END
                  < TIMESTAMPTZ '{(t0 + timedelta(days=1)).isoformat()}'""")
    else:
        con.execute("CREATE VIEW silver_day AS SELECT NULL::VARCHAR AS asset_id, NULL::VARCHAR AS tag_name WHERE false")
    rows = one("SELECT count(*) FROM silver_day")
    report = Report(day.isoformat(), rows)
    f = report.findings

    # G3-01 reconciliação: amostras no gold = linhas da silver, por ativo e tag (e o minuto bate com a hora)
    diff = con.execute("""
        WITH g AS (SELECT asset_id, tag_name, sum(samples) AS n FROM fact_tag_hour GROUP BY ALL),
             s AS (SELECT asset_id, tag_name, count(*) AS n FROM silver_day GROUP BY ALL)
        SELECT coalesce(g.asset_id, s.asset_id), coalesce(g.tag_name, s.tag_name), coalesce(g.n, 0), coalesce(s.n, 0)
        FROM g FULL JOIN s ON g.asset_id = s.asset_id AND g.tag_name = s.tag_name
        WHERE coalesce(g.n, 0) <> coalesce(s.n, 0) ORDER BY 1, 2""").fetchall()
    if diff:
        a, t, g, s = diff[0]
        f.append(Finding("G3-01", "blocker", len(diff), 0, f"{len(diff)} tags com amostras diferentes da silver "
                                                         f"(ex.: {a}.{t}: gold {g}, silver {s})"))
    minute, hour = one("SELECT sum(samples) FROM fact_tag_minute"), one("SELECT sum(samples) FROM fact_tag_hour")
    if minute != hour:
        f.append(Finding("G3-01", "blocker", minute or 0, hour or 0, f"fact_tag_minute soma {minute} amostras e fact_tag_hour {hour}"))

    # G3-02 buracos: 24 horas por ativo e por tag (uma hora sem dados aparece com cobertura 0, nunca falta a linha)
    for table, keys in (("fact_asset_hour", "asset_id"), ("fact_tag_hour", "asset_id, tag_name")):
        bad = con.execute(f"""SELECT {keys}, count(DISTINCT hour_utc) AS n FROM {table} GROUP BY ALL
                              HAVING n <> 24 OR min(hour_utc) <> TIMESTAMPTZ '{t0.isoformat()}'""").fetchall()
        if bad:
            f.append(Finding("G3-02", "blocker", len(bad), 0, f"{table}: {len(bad)} séries sem as 24 horas (ex.: {bad[0]})"))

    # G3-03 física: segundos, disponibilidade, energia, volume e KPIs dentro do possível
    seconds = one("""SELECT count(*) FROM fact_asset_hour WHERE run_s + starting_s + stopped_s + fault_s + maintenance_s
                     + no_data_s <> 3600 OR planned_s + planned_stop_s <> 3600 OR planned_with_data_s > planned_s
                     OR run_planned_s > planned_with_data_s""")
    if seconds:
        f.append(Finding("G3-03", "blocker", seconds, 0, f"{seconds} horas cujos segundos não somam 3600"))
    negative = one("""SELECT count(*) FROM fact_asset_hour WHERE energy_kwh < 0 OR coalesce(volume_m3, 0) < 0
                      OR useful_basis_kwh < 0 OR failures < 0 OR coalesce(alarms, 0) < 0""")
    if negative:
        f.append(Finding("G3-03", "blocker", negative, 0, f"{negative} horas com energia, volume ou contagens negativas"))
    lo, hi = KWH_PER_M3
    for table in ("kpi_asset_shift", "kpi_asset_day"):
        for asset, value, detail in con.execute(f"""
                SELECT asset_id, availability, 'disponibilidade fora de [0, 1]' FROM {table} WHERE availability NOT BETWEEN 0 AND 1
                UNION ALL SELECT asset_id, coverage, 'cobertura fora de [0, 1]' FROM {table} WHERE coverage NOT BETWEEN 0 AND 1
                UNION ALL SELECT asset_id, kwh_per_m3, 'kWh/m³ fora de [{lo}; {hi}]' FROM {table} WHERE kwh_per_m3 NOT BETWEEN {lo} AND {hi}
                UNION ALL SELECT asset_id, efficiency, 'rendimento fora de ]0, 1]' FROM {table} WHERE efficiency <= 0 OR efficiency > 1
                UNION ALL SELECT asset_id, cop, 'COP fora de ]0, {COP_MAX}]' FROM {table} WHERE cop <= 0 OR cop > {COP_MAX}
                ORDER BY 1, 3""").fetchall():
            f.append(Finding("G3-03", "blocker", round(value, 4), 0, f"{table}: {detail}", asset))

    # G3-07 estado desconhecido: o ativo fica "sem dados" o dia inteiro (nenhum segundo com estado), mas os seus sinais
    # têm leituras GOOD. Quase sempre é o estado inicial que falta (o gateway só escreve o `state` ao mudar), não uma
    # falha de comunicação. Foi o caso do MOTOR-01 na Oracle a 23-11: 100 % das leituras GOOD, a 30 kW e 1480 rpm, e
    # cobertura 0 nos KPIs. Aviso e não bloqueio: o gold está coerente com o que leu; quem o vê tem de saber porquê.
    for asset, good in con.execute("""
            WITH a AS (SELECT asset_id FROM fact_asset_hour GROUP BY 1 HAVING sum(no_data_s) = count(*) * 3600),
                 t AS (SELECT asset_id, sum(good_samples) AS good FROM fact_tag_hour GROUP BY 1)
            SELECT asset_id, t.good FROM a JOIN t USING (asset_id) WHERE t.good > 0 ORDER BY 1""").fetchall():
        f.append(Finding("G3-07", "warn", int(good), 0, f"o dia inteiro sem estado, mas com {good} leituras GOOD: "
                                                        "falta o estado inicial do sinal state?", asset))

    # G3-04 cobertura: horas do dia sem silver no main (recusadas pelo G2, ou que não correram)
    missing = sorted(set(range(24)) - set(silver_hours))
    if missing:
        severity = "blocker" if len(missing) > MISSING_HOURS_BLOCK else "warn"
        f.append(Finding("G3-04", severity, len(missing), MISSING_HOURS_BLOCK,
                         f"{len(missing)} horas sem silver no main: {', '.join(f'{h:02d}h' for h in missing[:8])}"))

    # G3-05 volume e G3-06 deriva: contra os dias anteriores que já estão no gold
    if history:
        con.execute("CREATE VIEW hist AS SELECT * FROM read_parquet([" + ", ".join(_lit(p) for p in history)
                    + "], hive_partitioning = false)")
        per_day = [r[0] for r in con.execute("SELECT sum(samples) FROM hist GROUP BY hour_utc::DATE ORDER BY 1").fetchall()]
        mean = sum(per_day) / len(per_day)
        change = (rows - mean) / mean if mean else 0.0
        if abs(change) > VOLUME_TOLERANCE:
            f.append(Finding("G3-05", "warn", round(change, 4), VOLUME_TOLERANCE,
                             f"{rows} amostras no dia contra uma média de {mean:.0f} nos {len(per_day)} dias anteriores ({change:+.0%})"))
    else:
        f.append(Finding("G3-05", "info", 0, VOLUME_TOLERANCE, "sem dias anteriores no gold para comparar o volume"))

    days = one("SELECT count(DISTINCT hour_utc::DATE) FROM hist") if history else 0
    if days < MIN_HISTORY_DAYS:
        f.append(Finding("G3-06", "info", days, MIN_HISTORY_DAYS, f"só {days} dias anteriores no gold: a deriva (PSI) ainda não se calcula"))
    else:
        analog = con.execute("""SELECT DISTINCT asset_id, tag_name FROM fact_tag_hour WHERE mean_value IS NOT NULL
                                AND tag_name NOT IN ('Connected', 'alarm', 'run_hours') ORDER BY 1, 2""").fetchall()
        for asset, tag in analog:
            ref = [r[0] for r in con.execute("SELECT mean_value FROM hist WHERE asset_id = ? AND tag_name = ?", [asset, tag]).fetchall()]
            cur = [r[0] for r in con.execute("SELECT mean_value FROM fact_tag_hour WHERE asset_id = ? AND tag_name = ?", [asset, tag]).fetchall()]
            value = psi(ref, cur)
            if value is not None and value > PSI_WARN:
                f.append(Finding("G3-06", "warn", round(value, 4), PSI_WARN, f"{tag}: a média horária mudou de distribuição (PSI {value:.2f})", asset))
    con.close()
    return report
