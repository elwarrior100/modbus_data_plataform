"""OpenMetadata: o catálogo do lake e o histórico dos gates (ADR-009, decisão 5).

O OpenMetadata não tem conector para o lakeFS, por isso somos nós a empurrar:
- o catálogo: serviço `lakefs` → base `industrial_lake` → tabelas `bronze.events` e `silver.readings`, com as colunas
  dos contratos (docs/bronze-contract.md, docs/silver-contract.md);
- a linhagem: bronze.events → (pipeline silver_hourly) → silver.readings;
- o G2 como testes de qualidade: um caso de teste por regra (G2-01 … G2-07) na tabela silver, e um resultado por hora.

Tudo é "melhor esforço": se o OpenMetadata estiver em baixo, o silver não falha (o gate que impõe é o hook do lakeFS;
o OpenMetadata só mostra e guarda o histórico). Usa só a biblioteca-padrão (urllib), sem dependências novas.
Configuração: OPENMETADATA_URL (ex.: http://openmetadata.openmetadata.svc.cluster.local:8585) e OPENMETADATA_TOKEN
(o JWT do bot de ingestão, vindo do cofre). Sem as duas, não faz nada.
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger(__name__)

SERVICE, DATABASE = "lakefs", "industrial_lake"
BRONZE = f"{SERVICE}.{DATABASE}.bronze.events"
SILVER = f"{SERVICE}.{DATABASE}.silver.readings"
PIPELINE_SERVICE, PIPELINE = "airflow-modbus", "silver_hourly"
TEST_DEFINITION = "g2Rule"
G2_RULES = {
    "G2-01": "completude: minutos da hora com pelo menos uma leitura GOOD, por ativo",
    "G2-02": "unicidade: bronze_event_id sem repetidos; (ativo, tag, instante) sem valores diferentes",
    "G2-03": "validade: linhas fora do intervalo válido do catálogo",
    "G2-04": "qualidade: linhas com qualidade que não é GOOD",
    "G2-05": "frescura: a última linha chegou perto do fim da hora, por ativo",
    "G2-06": "sensor parado: variável analógica sem mudanças durante a hora",
    "G2-07": "relógio: linhas com o relógio do dispositivo desalinhado",
}

BRONZE_COLUMNS = [
    ("event_id", "STRING", "identificador determinístico da amostra (UUIDv5)"),
    ("source", "STRING", "origem (gateway OPC UA)"),
    ("opcua_node_id", "STRING", "nó OPC UA, ex.: ns=2;s=PUMP-01.outlet_pressure"),
    ("raw_value", "STRING", "valor original em JSON (nulo se a qualidade é Bad)"),
    ("opcua_status_code", "INT", "StatusCode OPC UA (uint32)"),
    ("source_ts", "TIMESTAMP", "instante no dispositivo (UTC)"),
    ("server_ts", "TIMESTAMP", "instante no servidor OPC UA (UTC)"),
    ("ingest_ts", "TIMESTAMP", "instante em que o collector recebeu (UTC); define a partição"),
    ("schema_version", "STRING", "versão do contrato do bronze"),
    ("batch_id", "STRING", "lote do writer que a escreveu"),
]
SILVER_COLUMNS = [
    ("asset_id", "STRING", "ativo, ex.: PUMP-01"), ("tag_name", "STRING", "variável, ex.: outlet_pressure"),
    ("ts_utc", "TIMESTAMP", "source_ts, ou ingest_ts se faltar (flag ts_fallback)"),
    ("value_double", "DOUBLE", "valor numérico"), ("value_bool", "BOOLEAN", "valor booleano"),
    ("value_str", "STRING", "etiqueta do estado ou instante do device_clock"), ("unit", "STRING", "código UNECE"),
    ("quality", "STRING", "GOOD / UNCERTAIN / BAD"), ("quality_reason", "STRING", "nome do StatusCode"),
    ("dq_flags", "ARRAY", "avisos de qualidade da linha"),
    ("ingest_ts", "TIMESTAMP", "do bronze"), ("source_ts", "TIMESTAMP", "do bronze"), ("server_ts", "TIMESTAMP", "do bronze"),
    ("bronze_event_id", "STRING", "linhagem até à amostra original"), ("bronze_batch_id", "STRING", "lote do bronze"),
    ("silver_version", "STRING", "versão do código da transformação"),
]


class Client:
    def __init__(self, url: str, token: str, timeout: float = 10.0):
        self.base, self.token, self.timeout = url.rstrip("/") + "/api/v1", token, timeout

    def call(self, method: str, path: str, body=None, content_type: str = "application/json"):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": content_type, "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def put(self, path: str, body):            # PUT = criar ou atualizar (idempotente)
        return self.call("PUT", path, body)

    def get(self, path: str):
        return self.call("GET", path)


def from_env() -> "Client | None":
    url, token = os.environ.get("OPENMETADATA_URL"), os.environ.get("OPENMETADATA_TOKEN")
    return Client(url, token) if url and token else None


def _columns(cols):
    out = []
    for name, kind, desc in cols:
        c = {"name": name, "dataType": kind, "description": desc}
        if kind == "ARRAY":
            c["arrayDataType"] = "STRING"
        out.append(c)
    return out


def _link(fqn: str) -> str:
    return f"<#E::table::{fqn}>"


def ensure_catalog(c: Client) -> None:
    """Cria (ou mantém) o catálogo, a linhagem e os casos de teste do G2. Idempotente: corre em cada hora."""
    c.put("/services/databaseServices", {
        "name": SERVICE, "serviceType": "CustomDatabase",
        "description": "lakeFS (repositório industrial-lake) sobre SeaweedFS: bronze e silver em parquet",
        "connection": {"config": {"type": "CustomDatabase", "sourcePythonClass": "not.used"}}})
    c.put("/databases", {"name": DATABASE, "service": SERVICE, "description": "repositório lakeFS industrial-lake"})
    for schema, desc in (("bronze", "amostras tal como chegaram (Write)"), ("silver", "leituras limpas, uma partição por hora")):
        c.put("/databaseSchemas", {"name": schema, "database": f"{SERVICE}.{DATABASE}", "description": desc})
    bronze = c.put("/tables", {"name": "events", "databaseSchema": f"{SERVICE}.{DATABASE}.bronze", "tableType": "Regular",
                               "description": "bronze/dt=AAAA-MM-DD/hour=HH/*.parquet, escrito pelo writer (gate G1)",
                               "columns": _columns(BRONZE_COLUMNS)})
    silver = c.put("/tables", {"name": "readings", "databaseSchema": f"{SERVICE}.{DATABASE}.silver", "tableType": "Regular",
                               "description": "silver/dt=AAAA-MM-DD/hour=HH/data.parquet, escrito pelo silver_hourly (gate G2)",
                               "columns": _columns(SILVER_COLUMNS)})
    c.put("/services/pipelineServices", {
        "name": PIPELINE_SERVICE, "serviceType": "CustomPipeline", "description": "DAGs do projeto modbus no Airflow da plataforma",
        "connection": {"config": {"type": "CustomPipeline", "sourcePythonClass": "not.used"}}})
    pipeline = c.put("/pipelines", {"name": PIPELINE, "service": PIPELINE_SERVICE,
                                    "description": "bronze → silver de hora a hora, com o gate G2 antes do merge (WAP)"})
    c.put("/lineage", {"edge": {"fromEntity": {"id": bronze["id"], "type": "table"},
                                "toEntity": {"id": silver["id"], "type": "table"},
                                "lineageDetails": {"pipeline": {"id": pipeline["id"], "type": "pipeline"},
                                                   "description": "silver_hourly (pipelines/src/silver)"}}})
    c.put("/dataQuality/testDefinitions", {
        "name": TEST_DEFINITION, "displayName": "Regra do gate G2", "entityType": "TABLE", "testPlatforms": ["Other"],
        "description": "Uma regra do gate G2 (docs/gates-contract.md), avaliada pelo silver_hourly em cada hora",
        "parameterDefinition": []})
    for rule, desc in G2_RULES.items():
        c.put("/dataQuality/testCases", {"name": rule, "displayName": f"{rule} {desc.split(':')[0]}", "description": desc,
                                         "entityLink": _link(SILVER), "testDefinition": TEST_DEFINITION, "parameterValues": []})


def push_g2(c: Client, hour: datetime, report) -> None:
    """Um resultado por regra do G2 para esta hora. pass → Success; aviso ou bloqueio → Failed (o texto diz qual).

    Repetir uma hora (um "clear" no Airflow, ou mudar o minuto da DAG) volta a enviar o resultado com o mesmo instante:
    o OpenMetadata recusa o POST com 409 ("already exists"). Nesse caso atualiza-se esse resultado (PATCH), para o
    catálogo ficar com o resultado da última avaliação, e segue-se para a regra seguinte (antes, o 409 parava o envio
    das regras que faltavam; visto a 2026-11-22 na repetição da hora 21)."""
    ts = int(hour.timestamp() * 1000)
    by_rule: dict[str, list] = {}
    for f in report.findings:
        by_rule.setdefault(f.id, []).append(f)
    for rule in G2_RULES:
        found = by_rule.get(rule, [])
        blocker = any(f.severity == "blocker" for f in found)
        status = "Failed" if found else "Success"
        text = ("BLOQUEIO (o merge é recusado): " if blocker else "AVISO (não bloqueia): " if found else "ok") + \
               "; ".join(f"{(f.asset + ' ') if f.asset else ''}{f.detail}" for f in found)
        fqn = urllib.parse.quote(f"{SILVER}.{rule}", safe="")
        result = {"timestamp": ts, "testCaseStatus": status, "result": f"{report.partition}: {text}"[:900],
                  "testResultValue": [{"name": "achados", "value": str(len(found))}]}
        try:
            c.call("POST", f"/dataQuality/testCases/testCaseResults/{fqn}", result)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            c.call("PATCH", f"/dataQuality/testCases/testCaseResults/{fqn}/{ts}",
                   [{"op": "replace", "path": f"/{k}", "value": result[k]} for k in ("testCaseStatus", "result", "testResultValue")],
                   content_type="application/json-patch+json")


def publish(hour: datetime, report) -> None:
    """Chamado pelo silver_hourly depois do G2. Nunca levanta exceção: o OpenMetadata é para ver, não para impor."""
    c = from_env()
    if c is None:
        return
    try:
        ensure_catalog(c)
        push_g2(c, hour, report)
        log.info("OpenMetadata: catálogo e resultado do G2 de %s enviados (%s)", report.partition, report.status)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        log.warning("OpenMetadata indisponível ou recusou o pedido (%s): o silver continua", error)


def report_from_attestation(doc: dict):
    """Reconstrói o relatório do G2 a partir da atestação `_gates/g2/dt=…/hour=…json` (contrato dos gates, secção 3)."""
    from .g2 import Finding, Report
    return Report(doc["partition"], doc["rows"], [Finding(**f) for f in doc["rules"]])


if __name__ == "__main__":
    # Envia para o OpenMetadata atestações G2 já escritas (por exemplo, para preencher o histórico de horas processadas
    # antes de o envio existir). Uso: python -m silver.openmetadata atestacao1.json [atestacao2.json ...]
    import sys
    from datetime import timezone
    c = from_env()
    if c is None:
        sys.exit("defina OPENMETADATA_URL e OPENMETADATA_TOKEN")
    ensure_catalog(c)
    for path in sys.argv[1:]:
        doc = json.load(open(path))
        hour = datetime.strptime(doc["partition"], "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        push_g2(c, hour, report_from_attestation(doc))
        print(f"{doc['partition']}: {doc['status']} enviado")
