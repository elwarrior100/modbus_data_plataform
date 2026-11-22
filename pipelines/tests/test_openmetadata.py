"""OpenMetadata: o envio do G2 nunca falha a hora e traduz cada regra no estado certo."""
from datetime import datetime, timezone

from silver import openmetadata as om
from silver.g2 import Finding, Report

HOUR = datetime(2026, 11, 21, 19, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"id": "x"}

    def put(self, path, body):
        return self.call("PUT", path, body)


def test_without_configuration_it_does_nothing(monkeypatch):
    monkeypatch.delenv("OPENMETADATA_URL", raising=False)
    monkeypatch.delenv("OPENMETADATA_TOKEN", raising=False)
    om.publish(HOUR, Report("2026-11-21T19", 10))          # não levanta, não chama nada


def test_openmetadata_down_does_not_fail_the_hour(monkeypatch):
    monkeypatch.setenv("OPENMETADATA_URL", "http://127.0.0.1:9")   # porta fechada
    monkeypatch.setenv("OPENMETADATA_TOKEN", "x")
    om.publish(HOUR, Report("2026-11-21T19", 10))          # só regista um aviso


def test_each_rule_gets_its_status():
    c = FakeClient()
    report = Report("2026-11-21T19", 100, [
        Finding("G2-01", "blocker", 0.75, 0.8, "45 de 60 minutos com dados (75%)", "PUMP-01"),
        Finding("G2-03", "warn", 0.001, 0.0, "0.1% das linhas fora do intervalo válido")])
    om.push_g2(c, HOUR, report)
    results = {path.rsplit(".", 1)[-1]: body for _, path, body in c.calls}
    assert set(results) == set(om.G2_RULES)
    assert results["G2-01"]["testCaseStatus"] == "Failed" and "BLOQUEIO" in results["G2-01"]["result"]
    assert results["G2-03"]["testCaseStatus"] == "Failed" and "AVISO" in results["G2-03"]["result"]
    assert results["G2-02"]["testCaseStatus"] == "Success"
    assert all(b["timestamp"] == int(HOUR.timestamp() * 1000) for b in results.values())
