"""/health reports an otel block."""

from fastapi.testclient import TestClient
import loxo_llm_router as R
from loxo_llm_router import tracing


def test_health_has_otel_block():
    client = TestClient(R.app)
    data = client.get("/health").json()
    assert "otel" in data
    assert set(data["otel"]) == {"enabled", "endpoint", "service_name"}
    assert isinstance(data["otel"]["enabled"], bool)


def test_resolved_config_captures_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
    assert tracing.resolve_otel_config()["endpoint"] == "http://jaeger:4318"


def test_health_reports_resolved_endpoint_not_live_env(monkeypatch):
    """/health must report the config in force, not a post-start env edit.
    Config resolves once at import; an unrestarted env change is not live."""
    monkeypatch.setattr(R, "_OTEL_CFG",
                        {"service_name": "svc-x", "endpoint": "http://jaeger:4318"})
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://somewhere-else:4318")
    data = TestClient(R.app).get("/health").json()
    assert data["otel"]["endpoint"] == "http://jaeger:4318"
    assert data["otel"]["service_name"] == "svc-x"
