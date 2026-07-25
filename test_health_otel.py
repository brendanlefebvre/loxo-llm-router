"""/health reports an otel block."""

from fastapi.testclient import TestClient
import loxo_llm_router as R


def test_health_has_otel_block():
    client = TestClient(R.app)
    data = client.get("/health").json()
    assert "otel" in data
    assert set(data["otel"]) == {"enabled", "endpoint", "service_name"}
    assert isinstance(data["otel"]["enabled"], bool)


def test_health_otel_reports_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
    client = TestClient(R.app)
    data = client.get("/health").json()
    assert data["otel"]["endpoint"] == "http://jaeger:4318"
