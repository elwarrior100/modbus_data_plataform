"""Gate G2: integridade da silver de uma hora (docs/gates-contract.md, secção 2).

Lê o ficheiro silver da hora e devolve um relatório com as regras que correram. Não corrige nada:
só decide `pass`, `warn` ou `fail` (se alguma regra `blocker` falhou). O hook do lakeFS impõe o `fail`."""
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from .catalog import Tag
from .transform import _lit

# limiares: (aviso, bloqueio). Propostas do contrato, a calibrar com a semana sintética e horas reais.
COMPLETENESS = (0.98, 0.80)          # mínimo aceitável (abaixo disto: aviso / bloqueio)
OUT_OF_RANGE = (0.0, 0.05)           # máximo aceitável (acima disto: aviso / bloqueio)
NOT_GOOD = (0.01, 0.20)
CLOCK_SKEW = (0.0, 0.50)
FRESHNESS_MIN = 5                    # minutos antes do fim da hora


@dataclass
class Finding:
    id: str
    severity: str                    # "warn" | "blocker"
    value: float
    threshold: float
    detail: str
    asset: str | None = None


@dataclass
class Report:
    partition: str
    rows: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def status(self) -> str:
        severities = {f.severity for f in self.findings}
        return "fail" if "blocker" in severities else "warn" if "warn" in severities else "pass"

    @property
    def summary(self) -> str:
        if not self.findings:
            return "sem achados"
        return "; ".join(f"{f.id}{'/' + f.asset if f.asset else ''} {f.severity}: {f.detail}" for f in self.findings[:4])

    def attestation(self, files: dict[str, str]) -> bytes:
        """O ficheiro `_gates/g2/...json` que o hook do lakeFS lê (contrato, secção 3)."""
        doc = {"gate": "G2", "version": 1, "status": self.status, "partition": self.partition,
               "files": files, "rows": self.rows, "rules": [asdict(f) for f in self.findings], "summary": self.summary}
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()


def _low(findings, id_, value, limits, detail, asset=None):
    """Regra do tipo 'tem de ser pelo menos X': aviso/bloqueio quando fica abaixo."""
    warn, block = limits
    if value < block:
        findings.append(Finding(id_, "blocker", round(value, 4), block, detail, asset))
    elif value < warn:
        findings.append(Finding(id_, "warn", round(value, 4), warn, detail, asset))


def _high(findings, id_, value, limits, detail):
    """Regra do tipo 'tem de ser no máximo X': aviso quando passa o 1.º limite, bloqueio quando passa o 2.º."""
    warn, block = limits
    if value > block:
        findings.append(Finding(id_, "blocker", round(value, 4), block, detail))
    elif value > warn:
        findings.append(Finding(id_, "warn", round(value, 4), warn, detail))


def evaluate(silver_file: str | Path, hour: datetime, catalog: list[Tag]) -> Report:
    hour = hour.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = hour + timedelta(hours=1)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW s AS SELECT * FROM read_parquet({_lit(silver_file)})")
    rows = con.execute("SELECT count(*) FROM s").fetchall()[0][0]
    report = Report(hour.strftime("%Y-%m-%dT%H"), rows)
    f = report.findings
    if rows == 0:
        f.append(Finding("G2-01", "blocker", 0.0, 1.0, "a silver da hora está vazia"))
        return report

    # G2-02 unicidade
    dup_ids = con.execute("SELECT count(*) FROM (SELECT bronze_event_id FROM s GROUP BY 1 HAVING count(*) > 1)").fetchall()[0][0]
    if dup_ids:
        f.append(Finding("G2-02", "blocker", dup_ids, 0, f"{dup_ids} bronze_event_id repetidos"))
    dup_ts = con.execute("""SELECT count(*) FROM (SELECT 1 FROM s GROUP BY asset_id, tag_name, ts_utc
                            HAVING count(DISTINCT coalesce(CAST(value_double AS VARCHAR), CAST(value_bool AS VARCHAR), value_str)) > 1)""").fetchall()[0][0]
    if dup_ts:
        f.append(Finding("G2-02", "warn", dup_ts, 0, f"{dup_ts} (ativo, tag, instante) com valores diferentes"))

    # G2-01 completude, por ativo: minutos da hora (por ingest_ts) com pelo menos uma leitura GOOD.
    # Não usa o relógio do dispositivo: um recuo desse relógio faz o spool descartar amostras repetidas (ponto fraco 8).
    minutes = dict(con.execute("SELECT asset_id, count(DISTINCT date_trunc('minute', ingest_ts)) FROM s WHERE quality = 'GOOD' GROUP BY 1").fetchall())
    healthy = set()
    for asset in sorted({t.asset for t in catalog}):
        ratio = minutes.get(asset, 0) / 60.0
        _low(f, "G2-01", ratio, COMPLETENESS, f"{minutes.get(asset, 0)} de 60 minutos com dados ({ratio:.0%})", asset)
        if ratio >= COMPLETENESS[0]:
            healthy.add(asset)

    # G2-03 validade, G2-04 qualidade, G2-07 relógio (por partição)
    share = lambda cond: con.execute(f"SELECT count(*) FILTER (WHERE {cond}) / count(*) FROM s").fetchall()[0][0]  # noqa: E731
    pct = lambda v: f"{v:.2%}"  # noqa: E731
    v = share("list_contains(dq_flags, 'out_of_range')")
    _high(f, "G2-03", v, OUT_OF_RANGE, f"{pct(v)} das linhas fora do intervalo válido")
    v = share("quality != 'GOOD'")
    _high(f, "G2-04", v, NOT_GOOD, f"{pct(v)} das linhas com qualidade que não é GOOD")
    v = share("list_contains(dq_flags, 'clock_skew')")
    _high(f, "G2-07", v, CLOCK_SKEW, f"{pct(v)} das linhas com o relógio do dispositivo desalinhado")

    # G2-05 frescura, por ativo: a última linha chegou perto do fim da hora
    cutoff = end - timedelta(minutes=FRESHNESS_MIN)
    last = dict(con.execute("SELECT asset_id, max(ingest_ts) FROM s GROUP BY 1").fetchall())
    for asset in sorted({t.asset for t in catalog}):
        seen = last.get(asset)
        if seen is not None and seen.astimezone(timezone.utc) < cutoff:
            late = (end - seen.astimezone(timezone.utc)).total_seconds() / 60
            f.append(Finding("G2-05", "warn", round(late, 1), FRESHNESS_MIN, f"sem dados nos últimos {late:.0f} min da hora", asset))

    # G2-06 sensor parado: variável analógica sem nenhuma linha, num ativo com comunicação saudável
    present = set(con.execute("SELECT DISTINCT asset_id, tag_name FROM s").fetchall())
    for t in catalog:
        if t.kind == "analog" and t.asset in healthy and (t.asset, t.tag) not in present:
            f.append(Finding("G2-06", "warn", 0, 1, f"{t.tag} não mudou durante a hora (sensor parado?)", t.asset))
    return report
