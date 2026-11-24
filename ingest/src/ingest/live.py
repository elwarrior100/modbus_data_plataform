"""O caminho live (ADR-022, opção L1): cada amostra que entra no spool é copiada, já descodificada, para uma
hypertable do TimescaleDB (`live.readings`), de onde o Grafana lê "agora".

Como funciona: um trigger AFTER INSERT na tabela `events` do spool. Só corre para as linhas realmente novas (as
repetidas, `ON CONFLICT DO NOTHING`, não o disparam), dentro da mesma transação do collector, por isso a amostra fica
legível no Timescale no mesmo instante em que fica guardada no spool.

**Regra de ouro (condição da revisão independente):** o live é para VER; a verdade é o spool e o bronze. Por isso o
trigger nunca pode fazer falhar a escrita do collector:
- todo o corpo está dentro de `BEGIN … EXCEPTION WHEN OTHERS`: um erro (um valor que não converte, a hypertable em
  falta depois de um restauro, um bloqueio) vira um aviso no log do Postgres e um contador em `live.health`, e a
  amostra entra no spool na mesma;
- `lock_timeout = 100ms` na função: se a compressão ou a retenção do Timescale estiverem a segurar a hypertable, o
  trigger desiste depressa em vez de prender o collector;
- só se convertem os nós da tabela `live.nodes`, gerada a partir do `nodes.yaml` (o mesmo contrato do G1). Os
  números passam a `double precision`; os booleanos a 1/0; os instantes (`device_clock`) não entram. Um nó
  desconhecido é ignorado (quem o trata é o G1).

O instante de cada leitura é o do dispositivo (`source_ts`), a menos que o relógio dele esteja desalinhado mais de
60 s do gateway (a mesma regra `clock_skew` da silver, ADR-018); aí usa-se o do gateway, e por fim o da ingestão.

Retenção: 7 dias, comprimido ao fim de 1 dia (políticas do Timescale). O esquema `live` fica fora dos dumps: refaz-se
sozinho (o vigia do collector, `keep_installed`, verifica-o a cada 5 min e reinstala o que faltar) e o histórico está
no bronze e na silver.
"""
import logging
import threading

import psycopg

from .bronze import load_nodes

log = logging.getLogger("ingest.live")

LIVE = "live"
SKEW_US = 60 * 1_000_000            # o mesmo limite do clock_skew da silver (pipelines/src/silver/transform.py)
NUMERIC = {"double", "uint16", "uint32", "int16", "int32", "float"}


def node_rows(nodes: dict[str, str] | None = None) -> list[tuple[str, str, str, str]]:
    """(nó OPC UA, ativo, sinal, tipo) para cada nó que entra no live, a partir do nodes.yaml."""
    out = []
    for node_id, kind in (load_nodes() if nodes is None else nodes).items():
        if kind not in NUMERIC and kind != "boolean":
            continue                                   # datetime (device_clock): não é um número para um gráfico
        asset, tag = node_id.split(";s=", 1)[1].rsplit(".", 1)
        out.append((node_id, asset, tag, "boolean" if kind == "boolean" else "number"))
    return out


def _sql(live: str, ts: str) -> str:
    """`ts`: o esquema onde está a extensão timescaledb (normalmente public)."""
    return f"""
CREATE SCHEMA IF NOT EXISTS {live};
CREATE TABLE IF NOT EXISTS {live}.nodes (
    node_id TEXT PRIMARY KEY, asset TEXT NOT NULL, tag TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS {live}.health (
    id BOOLEAN PRIMARY KEY DEFAULT true CHECK (id), failures BIGINT NOT NULL DEFAULT 0,
    last_error TEXT, last_at TIMESTAMPTZ);
INSERT INTO {live}.health DEFAULT VALUES ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS {live}.readings (
    time TIMESTAMPTZ NOT NULL,
    asset TEXT NOT NULL,
    tag TEXT NOT NULL,
    value DOUBLE PRECISION,                -- NULL quando o OPC UA disse que a leitura é má (status em `status`)
    status BIGINT NOT NULL,
    ingest_time TIMESTAMPTZ NOT NULL,      -- quando o collector a recebeu: serve para medir a latência
    clock_skew BOOLEAN NOT NULL);
SELECT {ts}.create_hypertable('{live}.readings', {ts}.by_range('time', INTERVAL '1 day'), if_not_exists => true);
CREATE INDEX IF NOT EXISTS readings_asset_tag_time ON {live}.readings (asset, tag, time DESC);
ALTER TABLE {live}.readings SET (timescaledb.compress, timescaledb.compress_segmentby = 'asset, tag',
                                  timescaledb.compress_orderby = 'time DESC');
SELECT {ts}.add_compression_policy('{live}.readings', INTERVAL '1 day', if_not_exists => true);
SELECT {ts}.add_retention_policy('{live}.readings', INTERVAL '7 days', if_not_exists => true);

CREATE OR REPLACE FUNCTION live_copy() RETURNS trigger LANGUAGE plpgsql
SET lock_timeout = '100ms' AS $fn$
DECLARE
    n RECORD;
    v DOUBLE PRECISION;
    skew BOOLEAN;
    t BIGINT;
BEGIN
    BEGIN
        SELECT asset, tag, kind INTO n FROM {live}.nodes WHERE node_id = NEW.opcua_node_id;
        IF NOT FOUND THEN
            RETURN NULL;                                  -- nó desconhecido ou um instante: não é para o live
        END IF;
        IF NEW.raw_value IS NULL THEN
            v := NULL;
        ELSIF n.kind = 'boolean' THEN
            v := CASE NEW.raw_value WHEN 'true' THEN 1 WHEN 'false' THEN 0 END;
        ELSE
            v := NEW.raw_value::double precision;
        END IF;
        skew := NEW.source_ts IS NOT NULL AND NEW.server_ts IS NOT NULL
                AND abs(NEW.server_ts - NEW.source_ts) > {SKEW_US};
        t := coalesce(CASE WHEN skew THEN NULL ELSE NEW.source_ts END, NEW.server_ts, NEW.ingest_ts);
        INSERT INTO {live}.readings (time, asset, tag, value, status, ingest_time, clock_skew)
        VALUES (timestamptz 'epoch' + t * interval '1 microsecond', n.asset, n.tag, v, NEW.opcua_status_code,
                timestamptz 'epoch' + NEW.ingest_ts * interval '1 microsecond', skew);
    EXCEPTION WHEN OTHERS THEN
        -- nunca falha a escrita do spool: conta e avisa
        RAISE WARNING 'live: amostra % não copiada: %', NEW.event_id, SQLERRM;
        BEGIN
            UPDATE {live}.health SET failures = failures + 1, last_error = left(SQLERRM, 500), last_at = now();
        EXCEPTION WHEN OTHERS THEN
            NULL;                                         -- nem o contador existe (esquema live em falta)
        END;
    END;
    RETURN NULL;
END
$fn$;
CREATE OR REPLACE TRIGGER live_copy AFTER INSERT ON events FOR EACH ROW EXECUTE FUNCTION live_copy();
"""


def install(dsn: str, *, live: str = LIVE, nodes: dict[str, str] | None = None) -> None:
    """Cria (ou atualiza) o esquema live, a hypertable, a tabela de nós e o trigger, numa só transação.
    Idempotente. Precisa da extensão timescaledb já criada na base (scripts/bootstrap-postgres-ingest.sh)."""
    rows = node_rows(nodes)
    with psycopg.connect(dsn) as db, db.cursor() as cur:
        found = cur.execute("SELECT extnamespace::regnamespace::text FROM pg_extension "
                            "WHERE extname = 'timescaledb'").fetchone()
        if found is None:
            raise psycopg.errors.UndefinedObject("a extensão timescaledb não está criada nesta base")
        cur.execute(_sql(live, found[0]))
        cur.execute(f"DELETE FROM {live}.nodes WHERE node_id <> ALL(%s)", ([r[0] for r in rows],))
        cur.executemany(f"INSERT INTO {live}.nodes VALUES (%s,%s,%s,%s) ON CONFLICT (node_id) DO UPDATE "
                        "SET asset = EXCLUDED.asset, tag = EXCLUDED.tag, kind = EXCLUDED.kind", rows)


def try_install(dsn: str, *, live: str = LIVE) -> bool:
    """Melhor esforço: sem Timescale (ou com outro erro) devolve False e o collector continua na mesma."""
    try:
        install(dsn, live=live)
        log.info("caminho live instalado (trigger no spool → %s.readings)", live)
        return True
    except psycopg.Error as error:
        log.warning("caminho live não instalado (%s): o spool e o bronze continuam normais", error)
        return False


def installed(dsn: str, *, live: str = LIVE) -> bool:
    """O live está inteiro? A hypertable, o contador e o trigger ativo na `events`. Só leituras ao catálogo: não
    bloqueia a spool (o `install`, esse, troca o trigger e prende a `events` por um instante)."""
    with psycopg.connect(dsn, connect_timeout=5) as db:
        return db.execute(
            "SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL AND EXISTS ("
            "  SELECT FROM pg_trigger WHERE tgname = 'live_copy' AND tgrelid = to_regclass('events')"
            "  AND tgenabled <> 'D')",
            (f"{live}.readings", f"{live}.health")).fetchone()[0]


def ensure(dsn: str, *, live: str = LIVE) -> bool:
    """Uma volta do vigia: se o live estiver inteiro não faz nada; se não, instala. True = está inteiro."""
    try:
        if installed(dsn, live=live):
            return True
    except psycopg.Error as error:
        log.warning("live: não consegui ver se está instalado (%s)", error)
        return False
    return try_install(dsn, live=live)


def keep_installed(dsn: str, stop: threading.Event, *, every: float = 300, live: str = LIVE) -> None:
    """O vigia do collector (lição da promoção de 23-11): verifica já e depois a cada `every` segundos, e reinstala
    o que faltar. Cobre o collector que arranca antes de a extensão existir e um restauro que apague o esquema
    `live`, sem reiniciar o collector (um reinício perde as amostras desses segundos: o OPC UA não as guarda)."""
    healthy = None
    while not stop.is_set():
        ok = ensure(dsn, live=live)
        if ok != healthy and healthy is not None:
            log.info("live: %s", "reposto" if ok else "deixou de estar instalado; volto a tentar")
        healthy = ok
        stop.wait(every)


def start_keeper(dsn: str, *, every: float = 300) -> threading.Event:
    """Lança o vigia numa thread à parte (fora do ciclo asyncio do collector). Devolve o evento para o parar."""
    stop = threading.Event()
    threading.Thread(target=keep_installed, args=(dsn, stop), kwargs={"every": every},
                     name="live-keeper", daemon=True).start()
    return stop
