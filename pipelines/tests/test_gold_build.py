"""O gold de um dia (ADR-019, K3), calculado do dia sintético (tests/fixture_gold.py): o que tem de ser verdade."""
import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pytest
import yaml

from fixture_gold import SATURDAY, SUNDAY
from gold.build import LOOKBACK_HOURS, TABLES, build_day, gold_path, silver_window
from gold.plan import load_plan

ROOT = Path(__file__).resolve().parents[2]


def silver_files(lake, day, tmp: Path, skip=()) -> list[Path]:
    """Os ficheiros silver da janela do dia, copiados do lago para o disco (como o run.py fará)."""
    out = []
    for h in silver_window(day):
        path = f"silver/dt={h:%Y-%m-%d}/hour={h:%H}/data.parquet"
        if path in lake.silver() and h not in skip:
            local = tmp / path.replace("/", "_")
            local.write_bytes(lake.silver()[path])
            out.append(local)
    return out


def build(lake, tmp: Path, day=SUNDAY, previous=None, skip=()):
    out = tmp / f"out-{day}-{'p' if previous else 'n'}-{len(skip)}"
    kw = {}
    if previous is not None:
        kw = {"previous_tag_hour": previous.files[gold_path("fact_tag_hour", day.fromordinal(day.toordinal() - 1))],
              "previous_asset_hour": previous.files[gold_path("fact_asset_hour", day.fromordinal(day.toordinal() - 1))]}
    return build_day(silver_files(lake, day, tmp, skip), day, out, **kw)


def lookback_from(lake, tmp: Path, calls: list | None = None):
    """O `lookback` do build, como o run.py o faz: a silver de uma hora anterior à janela, ou None se não houver."""
    def look(hour):
        if calls is not None:
            calls.append(hour)
        path = f"silver/dt={hour:%Y-%m-%d}/hour={hour:%H}/data.parquet"
        if path not in lake.silver():
            return None
        local = tmp / ("lookback_" + path.replace("/", "_"))
        local.write_bytes(lake.silver()[path])
        return local
    return look


STATES = "SELECT asset_id, hour_utc, run_s, starting_s, stopped_s, fault_s, maintenance_s, no_data_s FROM T ORDER BY 1, 2"


@pytest.fixture(scope="module")
def days(gold_lake, tmp_path_factory):
    lake = gold_lake[0]
    tmp = tmp_path_factory.mktemp("gold")
    saturday = build(lake, tmp, SATURDAY)
    sunday = build(lake, tmp, SUNDAY, previous=saturday)
    return lake, tmp, saturday, sunday


def sql(result, table, query):
    """Corre `query` com a tabela gold `table` do resultado no lugar de `FROM T`."""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    src = f"read_parquet('{result.files[gold_path(table, result.day)]}', hive_partitioning = false)"
    return con.execute(query.replace("FROM T", "FROM " + src)).fetchall()


# --- o plano -------------------------------------------------------------------------------------------------------

def test_the_gold_plan_is_a_faithful_copy_of_the_simulator_plan():
    ours = yaml.safe_load((ROOT / "pipelines/src/gold/plan.yaml").read_text())
    theirs = yaml.safe_load((ROOT / "ot/simulator/src/ot_simulator/plan.yaml").read_text())
    assert ours == theirs                     # se o simulador mudar o plano, o gold tem de mudar com ele


def test_each_utc_hour_falls_in_one_lisbon_shift_and_shift_c_belongs_to_the_day_it_starts():
    plan = load_plan()
    winter = lambda h: plan.shift_of(datetime(2026, 11, 1, h, tzinfo=timezone.utc))         # noqa: E731  Lisboa = UTC
    assert [winter(h)[1] for h in range(24)] == list("CCCCCCAAAAAAAABBBBBBBBCC")
    assert winter(3)[0] == date(2026, 10, 31) and winter(23)[0] == date(2026, 11, 1)
    summer = plan.shift_of(datetime(2026, 7, 1, 5, tzinfo=timezone.utc))                     # 06:00 em Lisboa (UTC+1)
    assert summer == (date(2026, 7, 1), "A")
    # mudança de hora (domingo 28-03-2027, 01:00 UTC): o turno C de sábado 27-03 tem 7 horas reais e o dia de produção 23
    shift_c = lambda d: [p for p in plan.shift_periods(d) if p.shift == "C"][0]      # noqa: E731
    assert shift_c(date(2027, 3, 26)).hours == 8 and shift_c(date(2027, 3, 27)).hours == 7
    assert plan.day_period(date(2027, 3, 27)).hours == 23 and plan.day_period(date(2027, 3, 28)).hours == 24


# --- o gold do dia -------------------------------------------------------------------------------------------------

def test_every_table_is_written_with_a_fixed_name(days):
    _, _, _, sunday = days
    assert set(sunday.files) == {gold_path(t, SUNDAY) for t in TABLES}
    assert all(p.startswith("gold/") and p.endswith(f"/dt={SUNDAY}/data.parquet") for p in sunday.files)
    assert sunday.rows["fact_asset_hour"] == 72 and sunday.rows["fact_tag_hour"] == 29 * 24
    assert sunday.rows["fact_tag_minute"] == 29 * 1440 and sunday.rows["dim_time"] == 24


def test_repeating_the_day_gives_the_same_bytes(days):
    lake, tmp, saturday, sunday = days
    (tmp / "again").mkdir()
    again = build(lake, tmp / "again", SUNDAY, previous=saturday)
    digest = lambda r: {p: hashlib.sha256(f.read_bytes()).hexdigest() for p, f in r.files.items()}   # noqa: E731
    assert digest(again) == digest(sunday)


def test_the_samples_in_the_gold_are_exactly_the_silver_rows_of_the_day(days):
    lake, tmp, _, sunday = days
    files = ", ".join(f"'{f}'" for f in silver_files(lake, SUNDAY, tmp))
    expected = dict(((a, t), n) for a, t, n in duckdb.sql(f"""
        SELECT asset_id, tag_name, count(*) FROM read_parquet([{files}], hive_partitioning = false)
        WHERE CASE WHEN list_contains(dq_flags, 'clock_skew') THEN server_ts ELSE ts_utc END
              >= TIMESTAMPTZ '{SUNDAY} 00:00:00+00'
          AND CASE WHEN list_contains(dq_flags, 'clock_skew') THEN server_ts ELSE ts_utc END
              <  TIMESTAMPTZ '{SUNDAY} 00:00:00+00' + INTERVAL 1 DAY
        GROUP BY ALL""").fetchall())
    for table in ("fact_tag_hour", "fact_tag_minute"):
        got = {(a, t): n for a, t, n in sql(sunday, table, "SELECT asset_id, tag_name, sum(samples)::BIGINT FROM T GROUP BY ALL")}
        assert {k: v for k, v in got.items() if v} == expected
    assert sum(expected.values()) > 40_000                            # um dia inteiro (leituras a cada 30 s), não um bocado


def test_each_asset_has_24_hours_and_each_hour_has_3600_seconds(days):
    _, _, _, sunday = days
    rows = sql(sunday, "fact_asset_hour", """SELECT asset_id, count(*), min(run_s + starting_s + stopped_s + fault_s
              + maintenance_s + no_data_s), max(run_s + starting_s + stopped_s + fault_s + maintenance_s + no_data_s),
              min(planned_s + planned_stop_s), max(planned_s + planned_stop_s) FROM T GROUP BY 1""")
    assert sorted(r[0] for r in rows) == ["CHILLER-01", "MOTOR-01", "PUMP-01"]
    assert all(r[1:] == (24, 3600, 3600, 3600, 3600) for r in rows)


def test_downtime_intervals_have_the_right_cause_and_the_planned_part(days):
    _, _, _, sunday = days
    got = sql(sunday, "fact_downtime", """SELECT asset_id, strftime(start_utc, '%H:%M'), strftime(end_utc, '%H:%M'),
              cause, planned_s, from_previous_day FROM T ORDER BY 1, 2""")
    assert got == [
        ("CHILLER-01", "00:00", "06:00", "stopped", 21600, True),     # paragem planeada, vinda de sábado às 22:00
        ("CHILLER-01", "15:00", "15:10", "fault", 0, False),
        ("MOTOR-01", "10:00", "12:00", "maintenance", 7200, False),   # manutenção do plano
        ("MOTOR-01", "18:00", "18:20", "fault", 0, False),
        ("PUMP-01", "00:00", "00:10", "fault", 0, True),              # a avaria que atravessou a meia-noite
        ("PUMP-01", "06:00", "10:00", "maintenance", 14400, False),
        ("PUMP-01", "14:00", "14:15", "fault", 0, False),
    ]


def test_failures_are_entries_into_fault_and_the_comms_gap_is_no_data(days):
    _, _, saturday, sunday = days
    per_asset = dict((a, (f, al, nd)) for a, f, al, nd in sql(sunday, "fact_asset_hour",
                     "SELECT asset_id, sum(failures)::INT, sum(alarms)::INT, sum(no_data_s)::INT FROM T GROUP BY 1"))
    # a avaria da bomba que começou no sábado às 23:50 conta no sábado, não outra vez no domingo
    assert per_asset == {"PUMP-01": (1, 1, 0), "MOTOR-01": (1, 1, 600), "CHILLER-01": (1, None, 0)}
    assert sql(saturday, "fact_asset_hour", "SELECT sum(failures)::INT FROM T WHERE asset_id = 'PUMP-01'") == [(1,)]


def test_kpis_are_physical_and_close_to_what_the_simulator_does(days):
    _, _, _, sunday = days
    shifts = sql(sunday, "kpi_asset_shift", "SELECT asset_id, shift, complete, availability, coverage, efficiency, cop, kwh_per_m3 FROM T")
    assert len(shifts) == 9 and all(r[2] for r in shifts)            # C de sábado, A e B de domingo; todos completos
    for asset, shift, _, availability, coverage, efficiency, cop, kwh_m3 in shifts:
        assert availability is None or 0 <= availability <= 1
        assert 0 <= coverage <= 1
        if asset == "PUMP-01":
            assert 0.10 < kwh_m3 < 0.13 and 0.6 < efficiency < 0.7      # 25 kW, 220 m³/h; rendimento 0,65 no simulador
        if asset == "MOTOR-01":
            assert 0.9 < efficiency < 0.96                              # 0,93 no simulador
        if asset == "CHILLER-01" and cop is not None:
            assert 4.0 < cop < 5.5                                      # ~5 (menos a seguir à paragem: a água ainda aquece)
    day = {r[0]: r[1:] for r in sql(sunday, "kpi_asset_day", "SELECT asset_id, production_date, hours, complete, cop FROM T")}
    assert day["CHILLER-01"] == (date(2026, 10, 31), 24, True, pytest.approx(5.0, abs=0.15))   # o dia de produção de sábado


def test_mean_power_matches_the_register_map(days):
    _, _, _, sunday = days
    # horas a funcionar a hora toda: a média ponderada pelo tempo da potência (≈ 25 kW na bomba, ≈ 30 kW no motor)
    means = dict(sql(sunday, "fact_tag_hour", """SELECT asset_id, avg(mean_value) FROM T WHERE tag_name = 'power_kw'
                 AND hour_utc IN ('2026-11-01 02:00:00+00', '2026-11-01 03:00:00+00') GROUP BY 1"""))
    assert means["PUMP-01"] == pytest.approx(25, abs=2) and means["MOTOR-01"] == pytest.approx(30, abs=2)
    # parado pelo plano (o chiller de sábado 22:00 a domingo 06:00), a potência fica a 0 e o gateway deixa de a escrever:
    # vale 0 kW até à leitura seguinte, enquanto há comunicação (decisão de 23-11), com a hora toda coberta
    assert means["CHILLER-01"] == 0.0
    cover = dict(sql(sunday, "fact_tag_hour", """SELECT tag_name, min(seconds_with_data) FROM T WHERE asset_id = 'CHILLER-01'
                 AND tag_name IN ('power_kw', 'chw_flow') AND hour_utc IN ('2026-11-01 02:00:00+00', '2026-11-01 03:00:00+00')
                 GROUP BY 1"""))
    assert cover == {"power_kw": 3600, "chw_flow": 3600}


def test_slow_signals_use_their_own_hold(days):
    """As temperaturas mudam pouco para a resolução de 0,1 °C: com o teto de 300 s do catálogo ficam cobertas."""
    _, _, _, sunday = days
    cover = sql(sunday, "fact_tag_hour", """SELECT sum(seconds_with_data) / (24 * 3600.0) FROM T
                WHERE asset_id = 'CHILLER-01' AND tag_name = 'inlet_temp'""")[0][0]
    assert cover > 0.99


def test_without_the_previous_day_a_long_unchanged_state_starts_without_data(days):
    """O chiller está OFF desde sábado às 22:00 e o gateway só escreve ao mudar: sem o gold de sábado, o domingo não
    sabe o estado até ele mudar (às 06:00). É honesto ("sem dados"), mas por isso os dias calculam-se por ordem."""
    lake, tmp, _, sunday = days
    alone = build(lake, tmp, SUNDAY)
    q = "SELECT stopped_s, no_data_s FROM T WHERE asset_id = 'CHILLER-01' AND hour_utc = '2026-11-01 00:00:00+00'"
    assert sql(sunday, "fact_asset_hour", q) == [(3600, 0)]
    assert sql(alone, "fact_asset_hour", q) == [(0, 3600)]
    assert not any(r[0] for r in sql(alone, "kpi_asset_shift", "SELECT complete FROM T WHERE shift = 'C'"))


def test_an_hour_refused_by_g2_shows_up_as_an_hour_without_data(days):
    lake, tmp, saturday, _ = days
    hole = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    gap = build(lake, tmp, SUNDAY, previous=saturday, skip={hole})
    rows = sql(gap, "fact_asset_hour", "SELECT asset_id, no_data_s, energy_s FROM T WHERE hour_utc = '2026-11-01 12:00:00+00'")
    # os primeiros segundos ainda valem as últimas leituras das 11h (no máximo 60 s); o resto da hora é "sem dados"
    assert sorted(r[0] for r in rows) == ["CHILLER-01", "MOTOR-01", "PUMP-01"]
    assert all(3540 <= nd < 3600 and es <= 60 for _, nd, es in rows)
    assert gap.rows["fact_asset_hour"] == 72                            # a hora está lá, com cobertura 0


# --- o estado inicial que falta (Oracle, 23-11: o MOTOR-01 "sem dados" o dia inteiro) --------------------------------

def test_without_the_previous_day_the_lookback_finds_the_state_in_the_older_silver(days):
    """O 1.º dia do gold não tem D−1: o estado em vigor procura-se na silver anterior à janela, e o dia fica igual ao
    calculado com o gold de sábado (o chiller OFF desde sábado às 22:00 já conta como parado à meia-noite)."""
    lake, tmp, _, sunday = days
    calls = []
    found = build_day(silver_files(lake, SUNDAY, tmp), SUNDAY, tmp / "out-lookback", lookback=lookback_from(lake, tmp, calls))
    q = "SELECT stopped_s, no_data_s FROM T WHERE asset_id = 'CHILLER-01' AND hour_utc = '2026-11-01 00:00:00+00'"
    assert sql(found, "fact_asset_hour", q) == [(3600, 0)]
    assert sql(found, "fact_asset_hour", STATES) == sql(sunday, "fact_asset_hour", STATES)
    assert calls[0] == datetime(2026, 10, 31, 22, tzinfo=timezone.utc)     # começa logo antes da janela (D−1 23h)
    assert len(calls) <= LOOKBACK_HOURS


def test_with_the_previous_day_nothing_more_is_read(days):
    """No dia a dia o estado inicial vem do gold de D−1: nenhum pedido a mais ao Object Storage."""
    lake, tmp, saturday, sunday = days
    calls = []
    out = build_day(silver_files(lake, SUNDAY, tmp), SUNDAY, tmp / "out-prev-lookback", lookback=lookback_from(lake, tmp, calls),
                    previous_tag_hour=saturday.files[gold_path("fact_tag_hour", SATURDAY)],
                    previous_asset_hour=saturday.files[gold_path("fact_asset_hour", SATURDAY)])
    assert calls == [] and sql(out, "fact_asset_hour", STATES) == sql(sunday, "fact_asset_hour", STATES)


def _previous_with(saturday, tmp: Path, name: str, select: str) -> Path:
    src = f"read_parquet('{saturday.files[gold_path('fact_tag_hour', SATURDAY)]}', hive_partitioning = false)"
    out = tmp / f"prev-{name}.parquet"
    duckdb.sql(f"COPY ({select.replace('FROM T', 'FROM ' + src)}) TO '{out}' (FORMAT PARQUET)")
    return out


def test_a_state_lost_at_the_end_of_the_previous_day_is_looked_up_but_a_silent_one_is_not(days):
    """Com o gold de D−1, mas o `value_end` do estado nulo (a comunicação caiu no último segundo): procura-se a última
    leitura e o estado continua. Um sinal que esteve calado o dia D−1 inteiro não se procura outra vez (o build de D−1
    já o fez): sem isto, um sinal que nunca escreve custava 168 pedidos por dia."""
    lake, tmp, saturday, _ = days
    chiller = "asset_id = 'CHILLER-01' AND tag_name = 'state'"
    q = "SELECT stopped_s, no_data_s FROM T WHERE asset_id = 'CHILLER-01' AND hour_utc = '2026-11-01 00:00:00+00'"
    lost = _previous_with(saturday, tmp, "lost", f"SELECT * REPLACE (CASE WHEN {chiller} THEN NULL ELSE value_end END AS value_end) FROM T")
    silent = _previous_with(saturday, tmp, "silent", f"""SELECT * REPLACE (CASE WHEN {chiller} THEN NULL ELSE value_end END AS value_end,
        CASE WHEN {chiller} THEN 0 ELSE samples END AS samples,
        CASE WHEN {chiller} THEN 0 ELSE seconds_with_data END AS seconds_with_data) FROM T""")
    results = {}
    for name, previous in (("lost", lost), ("silent", silent)):
        calls = []
        out = build_day(silver_files(lake, SUNDAY, tmp), SUNDAY, tmp / f"out-{name}", previous_tag_hour=previous,
                        lookback=lookback_from(lake, tmp, calls))
        results[name] = (sql(out, "fact_asset_hour", q)[0], len(calls))
    assert results["lost"] == ((3600, 0), 1)                 # encontrado na primeira hora lida (sábado 22h)
    assert results["silent"] == ((0, 3600), 0)


def test_a_bad_stamped_by_the_gateway_does_not_beat_the_good_reading_that_came_after_it(days):
    """ADR-018: dentro do mesmo segundo ordena-se pelo `server_ts`. Ao religar (Oracle, 22-11 22:29:08) o gateway manda
    um BAD carimbado com milissegundos (.628) e logo a seguir o valor do PLC, com o `ts_utc` do segundo (.000) e um
    `server_ts` posterior (.843). O estado em vigor é o segundo, não o BAD."""
    lake, tmp, saturday, sunday = days
    files = silver_files(lake, SUNDAY, tmp)
    hour3 = next(f for f in files if "dt=2026-11-01_hour=03" in f.name)
    src = f"read_parquet('{hour3}', hive_partitioning = false)"
    running = f"(SELECT * FROM read_parquet([{', '.join(repr(str(f)) for f in files)}], union_by_name = true, " \
              "hive_partitioning = false) WHERE asset_id = 'MOTOR-01' AND tag_name = 'state' AND value_str = 'RUNNING' LIMIT 1)"
    at = lambda ms: f"TIMESTAMPTZ '2026-11-01 03:30:00.{ms:03d}+00'"         # noqa: E731
    patched = tmp / "silver-hour3-reconnect.parquet"
    duckdb.sql(f"""COPY (
        SELECT * FROM {src}
        UNION ALL BY NAME SELECT * REPLACE ({at(628)} AS ts_utc, {at(628)} AS server_ts, 'BAD' AS quality,
                                            NULL::DOUBLE AS value_double, NULL::VARCHAR AS value_str,
                                            'reconnect-bad' AS bronze_event_id, dq_flags[1:0] AS dq_flags) FROM {running}
        UNION ALL BY NAME SELECT * REPLACE ({at(0)} AS ts_utc, {at(843)} AS server_ts, 'reconnect-good' AS bronze_event_id,
                                            dq_flags[1:0] AS dq_flags) FROM {running}
        ) TO '{patched}' (FORMAT PARQUET)""")
    out = build_day([patched if f == hour3 else f for f in files], SUNDAY, tmp / "out-reconnect",
                    previous_tag_hour=saturday.files[gold_path("fact_tag_hour", SATURDAY)],
                    previous_asset_hour=saturday.files[gold_path("fact_asset_hour", SATURDAY)])
    q = "SELECT run_s, no_data_s FROM T WHERE asset_id = 'MOTOR-01' AND hour_utc = '2026-11-01 03:00:00+00'"
    assert sql(sunday, "fact_asset_hour", q) == [(3600, 0)]
    assert sql(out, "fact_asset_hour", q) == [(3600, 0)]           # com a ordem antiga: (1800, 1800)
