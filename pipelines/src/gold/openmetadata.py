"""OpenMetadata do gold (ADR-019, D10, opção M1): catálogo, linhagem, histórico do G3 e a release em cada tabela.

Como o da silver (`silver.openmetadata`), é "melhor esforço": se o OpenMetadata estiver em baixo, o gold e as releases
não falham (quem impõe são os hooks do lakeFS; o catálogo só mostra e guarda o histórico).
- **Catálogo:** a base `industrial_lake` ganha o esquema `gold` com as 9 tabelas; as colunas vêm do próprio ficheiro
  parquet do dia (assim nunca divergem do que o `gold/build.py` escreve).
- **Linhagem:** silver.readings → (pipeline gold_daily) → cada tabela do gold.
- **G3:** um caso de teste por regra (G3-01 … G3-07), na tabela `gold.fact_tag_hour` (a do G3-01, a reconciliação com a
  silver), com um resultado por dia. Repetir um dia atualiza o resultado (409 → PATCH, como no G2).
- **Release (M1):** duas propriedades próprias em cada tabela do gold, `release` e `lakefs_commit`: a release para onde o
  `LATEST_STABLE` aponta (o que os consumidores veem). Mudam a cada release e a cada rollback. O histórico das releases
  fica no lakeFS e nos manifests, não no catálogo (por isso não se acumulam 365 etiquetas por ano, como na opção M2).
"""
import io
import logging
import urllib.error
import urllib.parse
from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from silver.openmetadata import DATABASE, PIPELINE_SERVICE, SERVICE, SILVER, Client, _link, from_env

from .build import TABLES, gold_path

log = logging.getLogger(__name__)

SCHEMA = f"{SERVICE}.{DATABASE}.gold"
PIPELINE = "gold_daily"
G3_TABLE = f"{SCHEMA}.fact_tag_hour"
TEST_DEFINITION = "g3Rule"
PROPERTIES = {"release": "release do gold para onde o LATEST_STABLE aponta (ADR-019, D9)",
              "lakefs_commit": "commit do lakeFS dessa release (a tag aponta para ele)"}
G3_RULES = {
    "G3-01": "reconciliação: amostras no gold = linhas da silver, por ativo e tag",
    "G3-02": "buracos: 24 horas por ativo e tag, nunca falta a linha",
    "G3-03": "física: segundos, disponibilidade, energia, rendimentos e COP dentro do possível",
    "G3-04": "cobertura: horas do dia sem silver no main",
    "G3-05": "volume: amostras do dia contra a média dos 7 dias anteriores",
    "G3-06": "deriva: PSI das médias horárias contra os dias anteriores",
    "G3-07": "estado desconhecido: o dia inteiro sem estado, mas com leituras GOOD (ADR-024)",
}
DESCRIPTIONS = {
    "dim_asset": "ativos e o que se sabe deles (tipo, potência, estados de funcionamento)",
    "dim_tag": "sinais do catálogo (unidade, intervalo, validade de uma leitura)",
    "dim_time": "hora UTC → data e turno de Lisboa (plano de produção)",
    "fact_tag_minute": "por sinal e minuto: amostras, cobertura, mínimo, máximo, média ponderada pelo tempo",
    "fact_tag_hour": "por sinal e hora: como o minuto, mais primeiro e último valor",
    "fact_asset_hour": "por ativo e hora: segundos por estado e sem dados, falhas, alarmes, energia, volume",
    "fact_downtime": "intervalos de paragem com causa (avaria, paragem planeada, manutenção, sem dados)",
    "kpi_asset_shift": "KPIs por ativo e turno: disponibilidade, MTBF, MTTR, kWh/m³, rendimentos, COP",
    "kpi_asset_day": "KPIs por ativo e dia de produção (os mesmos do turno)",
}


def _om_type(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "BOOLEAN"
    if pa.types.is_integer(t):
        return "BIGINT"
    if pa.types.is_floating(t):
        return "DOUBLE"
    if pa.types.is_timestamp(t):
        return "TIMESTAMP"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return "ARRAY"
    return "STRING"


def columns_from_parquet(data: bytes) -> list[dict]:
    """As colunas de uma tabela do gold, lidas do esquema do ficheiro (só o rodapé do parquet)."""
    out = []
    for f in pq.ParquetFile(io.BytesIO(data)).schema_arrow:
        c = {"name": f.name, "dataType": _om_type(f.type)}
        if c["dataType"] == "ARRAY":
            c["arrayDataType"] = "STRING"
        out.append(c)
    return out


def _ensure_properties(c: Client) -> None:
    """As duas propriedades próprias das tabelas (`release`, `lakefs_commit`), uma vez por instalação."""
    table = c.get("/metadata/types/name/table?fields=customProperties")
    have = {p["name"] for p in table.get("customProperties") or []}
    string_id = c.get("/metadata/types/name/string")["id"]
    for name, desc in PROPERTIES.items():
        if name not in have:
            c.put(f"/metadata/types/{table['id']}", {"name": name, "description": desc,
                                                     "propertyType": {"id": string_id, "type": "type"}})


def ensure_catalog(c: Client, lake, ref: str, day: date) -> None:
    """Cria (ou mantém) o esquema gold, as 9 tabelas, a linhagem e os casos de teste do G3. Idempotente."""
    _ensure_properties(c)
    c.put("/databaseSchemas", {"name": "gold", "database": f"{SERVICE}.{DATABASE}",
                               "description": "números de negócio por dia (ADR-019), em releases com tag e manifest"})
    pipeline = c.put("/pipelines", {"name": PIPELINE, "service": PIPELINE_SERVICE,
                                    "description": "silver → gold de um dia às 02:40 UTC, com o gate G3 e a release"})
    silver = c.get(f"/tables/name/{urllib.parse.quote(SILVER, safe='')}")
    for table in TABLES:
        cols = columns_from_parquet(lake.read(ref, gold_path(table, day)))
        t = c.put("/tables", {"name": table, "databaseSchema": SCHEMA, "tableType": "Regular", "columns": cols,
                              "description": f"gold/{table}/dt=AAAA-MM-DD/data.parquet: {DESCRIPTIONS[table]}"})
        c.put("/lineage", {"edge": {"fromEntity": {"id": silver["id"], "type": "table"},
                                    "toEntity": {"id": t["id"], "type": "table"},
                                    "lineageDetails": {"pipeline": {"id": pipeline["id"], "type": "pipeline"},
                                                       "description": "gold_daily (pipelines/src/gold)"}}})
    c.put("/dataQuality/testDefinitions", {
        "name": TEST_DEFINITION, "displayName": "Regra do gate G3", "entityType": "TABLE", "testPlatforms": ["Other"],
        "description": "Uma regra do gate G3 (docs/gates-contract.md §6), avaliada pelo gold_daily em cada dia",
        "parameterDefinition": []})
    for rule, desc in G3_RULES.items():
        c.put("/dataQuality/testCases", {"name": rule, "displayName": f"{rule} {desc.split(':')[0]}", "description": desc,
                                         "entityLink": _link(G3_TABLE), "testDefinition": TEST_DEFINITION, "parameterValues": []})


def push_g3(c: Client, day: date, att: dict) -> None:
    """Um resultado por regra do G3 para o dia (da atestação). Sem achados → Success; aviso ou bloqueio → Failed."""
    ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    by_rule: dict[str, list] = {}
    for f in att.get("rules", []):
        by_rule.setdefault(f["id"], []).append(f)
    for rule in G3_RULES:
        found = [f for f in by_rule.get(rule, []) if f.get("severity") in ("warn", "blocker")]
        blocker = any(f["severity"] == "blocker" for f in found)
        text = ("BLOQUEIO (o merge é recusado): " if blocker else "AVISO (não bloqueia): " if found else "ok") + \
               "; ".join(f"{(f.get('asset') + ' ') if f.get('asset') else ''}{f.get('detail', '')}" for f in found)
        fqn = urllib.parse.quote(f"{G3_TABLE}.{rule}", safe="")
        result = {"timestamp": ts, "testCaseStatus": "Failed" if found else "Success",
                  "result": f"{day.isoformat()}: {text}"[:900], "testResultValue": [{"name": "achados", "value": str(len(found))}]}
        try:
            c.call("POST", f"/dataQuality/testCases/testCaseResults/{fqn}", result)
        except urllib.error.HTTPError as error:
            if error.code != 409:                # o dia já tinha resultado: atualiza-se (repetir o dia)
                raise
            c.call("PATCH", f"/dataQuality/testCases/testCaseResults/{fqn}/{ts}",
                   [{"op": "replace", "path": f"/{k}", "value": result[k]} for k in ("testCaseStatus", "result", "testResultValue")],
                   content_type="application/json-patch+json")


def set_release(c: Client, tag: str, commit: str) -> None:
    """As propriedades `release` e `lakefs_commit` de cada tabela do gold passam a dizer a release do LATEST_STABLE."""
    for table in TABLES:
        t = c.get(f"/tables/name/{urllib.parse.quote(f'{SCHEMA}.{table}', safe='')}")
        c.call("PATCH", f"/tables/{t['id']}", [{"op": "add", "path": "/extension",
                                                "value": {"release": tag, "lakefs_commit": commit}}],
               content_type="application/json-patch+json")


def publish(lake, day: date, *, ref: str = "main", release: tuple[str, str] | None = None) -> None:
    """Chamado pelo gold_daily depois do gold e da release. Nunca levanta exceção (o OpenMetadata é para ver)."""
    c = from_env()
    if c is None:
        return
    try:
        import json
        ensure_catalog(c, lake, ref, day)
        push_g3(c, day, json.loads(lake.read(ref, f"_gates/g3/dt={day.isoformat()}.json")))
        if release:
            set_release(c, *release)
        log.info("OpenMetadata: gold de %s, G3 e release %s enviados", day, release[0] if release else "-")
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        log.warning("OpenMetadata indisponível ou recusou o pedido (%s): o gold continua", error)


def publish_release(tag: str, commit: str) -> None:
    """Depois de mover o LATEST_STABLE (release nova ou rollback): só as duas propriedades. Melhor esforço."""
    c = from_env()
    if c is None:
        return
    try:
        set_release(c, tag, commit)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        log.warning("OpenMetadata: não consegui atualizar a release (%s)", error)
