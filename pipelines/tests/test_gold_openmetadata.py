"""OpenMetadata do gold (ADR-019 D10, M1): catálogo das 9 tabelas, linhagem, G3 e a release em cada tabela.
Com um cliente falso; o envio real prova-se contra o OpenMetadata local (ver o relatório da sessão de 23-11)."""
import copy
import io
import json
import urllib.error

import pytest

from fixture_gold import SATURDAY
from gold import openmetadata as om
from gold.build import TABLES
from gold.run import run_day


class FakeClient:
    def __init__(self, existing_results=()):
        self.calls, self.existing = [], set(existing_results)

    def call(self, method, path, body=None, content_type="application/json"):
        self.calls.append((method, path, body))
        if method == "POST" and "testCaseResults" in path and path.rsplit(".", 1)[-1] in self.existing:
            raise urllib.error.HTTPError(path, 409, "Conflict", {}, io.BytesIO(b"{}"))
        if path.startswith("/metadata/types/name/"):
            return {"id": "type-" + path.split("/")[4].split("?")[0], "customProperties": []}
        return {"id": "id-" + path.rsplit("/", 1)[-1]}

    def put(self, path, body):
        return self.call("PUT", path, body)

    def get(self, path):
        return self.call("GET", path)


@pytest.fixture(scope="module")
def lake(gold_lake):
    lake = copy.deepcopy(gold_lake[0])
    run_day(lake, SATURDAY)
    return lake


def test_the_catalog_has_the_nine_tables_with_their_columns_and_lineage(lake):
    c = FakeClient()
    om.ensure_catalog(c, lake, "main", SATURDAY)
    tables = {b["name"]: b for m, p, b in c.calls if m == "PUT" and p == "/tables"}
    assert set(tables) == set(TABLES)
    assert all(t["databaseSchema"] == om.SCHEMA and t["columns"] for t in tables.values())
    kpi = {col["name"]: col["dataType"] for col in tables["kpi_asset_day"]["columns"]}
    assert kpi["asset_id"] == "STRING" and "DOUBLE" in kpi.values()
    lineage = [b for m, p, b in c.calls if p == "/lineage"]
    assert len(lineage) == len(TABLES)                    # silver.readings → cada tabela do gold, pelo gold_daily
    cases = {b["name"] for m, p, b in c.calls if p == "/dataQuality/testCases"}
    assert cases == set(om.G3_RULES)
    props = {b["name"] for m, p, b in c.calls if m == "PUT" and p.startswith("/metadata/types/")}
    assert props == set(om.PROPERTIES)                    # release e lakefs_commit, criadas uma vez


def test_each_g3_rule_gets_its_result_and_a_repeated_day_is_updated(lake):
    att = json.loads(lake.read("main", f"_gates/g3/dt={SATURDAY}.json"))
    att["rules"] = att["rules"] + [{"id": "G3-05", "severity": "warn", "asset": None, "detail": "volume +25%", "value": 0.25, "threshold": 0.2}]
    c = FakeClient(existing_results={"G3-01"})            # o G3-01 deste dia já tinha resultado (repetir o dia)
    om.push_g3(c, SATURDAY, att)
    posts = {p.rsplit(".", 1)[-1]: b for m, p, b in c.calls if m == "POST"}
    assert set(posts) == set(om.G3_RULES)
    assert posts["G3-05"]["testCaseStatus"] == "Failed" and "AVISO" in posts["G3-05"]["result"]
    assert posts["G3-02"]["testCaseStatus"] == "Success"
    patches = [p for m, p, b in c.calls if m == "PATCH"]
    assert len(patches) == 1 and "G3-01" in patches[0]


def test_the_release_goes_to_every_gold_table():
    c = FakeClient()
    om.set_release(c, "gold-v1.0.0_202611240240", "abc123")
    patches = [b for m, p, b in c.calls if m == "PATCH"]
    assert len(patches) == len(TABLES)
    assert all(b == [{"op": "add", "path": "/extension", "value": {"release": "gold-v1.0.0_202611240240",
                                                                    "lakefs_commit": "abc123"}}] for b in patches)


def test_without_configuration_nothing_is_sent(lake, monkeypatch):
    monkeypatch.delenv("OPENMETADATA_URL", raising=False)
    monkeypatch.delenv("OPENMETADATA_TOKEN", raising=False)
    om.publish(lake, SATURDAY)                            # não levanta, não chama nada
    om.publish_release("gold-v1.0.0_202611240240", "abc")


@pytest.mark.skipif("OPENMETADATA_URL" not in __import__("os").environ, reason="sem OpenMetadata")
def test_publish_to_a_real_openmetadata(lake):
    """Contra um OpenMetadata real (o LOCAL; nunca o da Oracle nos testes): catálogo, G3 e a release nas tabelas."""
    import urllib.parse
    om.publish(lake, SATURDAY, release=("gold-v0.0.0_teste", "commit-de-teste"))
    c = om.from_env()
    t = c.get(f"/tables/name/{urllib.parse.quote(om.SCHEMA + '.kpi_asset_day', safe='')}?fields=extension,columns")
    assert t["extension"] == {"release": "gold-v0.0.0_teste", "lakefs_commit": "commit-de-teste"}
    assert {col["name"] for col in t["columns"]} >= {"asset_id", "production_date"}
    fqn = urllib.parse.quote(f"{om.G3_TABLE}.G3-01", safe="")
    results = c.get(f"/dataQuality/testCases/testCaseResults/{fqn}?startTs=0&endTs=9999999999999")["data"]
    assert any(r["result"].startswith(SATURDAY.isoformat()) for r in results)
    om.publish(lake, SATURDAY)                            # repetir o dia não falha (409 → PATCH)
