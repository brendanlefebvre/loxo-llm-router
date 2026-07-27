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


def test_signal_specific_endpoint_alone_enables_tracing(monkeypatch):
    """OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is a complete, valid OTel config on its
    own — the exporter honors it. Gating only on the generic var would leave such
    a deployment silently traceless."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://t:4318/v1/traces")
    cfg = tracing.resolve_otel_config()
    assert cfg is not None
    assert cfg["endpoint"] == "http://t:4318/v1/traces"


def test_signal_specific_endpoint_wins_over_generic(monkeypatch):
    """Mirrors the exporter's own precedence — /health must not name a URL the
    exporter isn't using."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://generic:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://traces:4318/v1/traces")
    assert tracing.resolve_otel_config()["endpoint"] == "http://traces:4318/v1/traces"


def test_health_prefers_the_exporters_resolved_endpoint(monkeypatch):
    """With LOXO_OTEL_ENABLED and no endpoint var, the exporter falls back to its
    own default; /health reports where spans actually go, not the unset config."""
    monkeypatch.setattr(R, "_OTEL_CFG", {"service_name": "svc", "endpoint": None})
    monkeypatch.setattr(R.TRACES, "endpoint", "http://localhost:4318/v1/traces")
    data = TestClient(R.app).get("/health").json()
    assert data["otel"]["endpoint"] == "http://localhost:4318/v1/traces"


def test_health_reports_resolved_endpoint_not_live_env(monkeypatch):
    """/health must report the config in force, not a post-start env edit.
    Config resolves once at import; an unrestarted env change is not live."""
    monkeypatch.setattr(R, "_OTEL_CFG",
                        {"service_name": "svc-x", "endpoint": "http://jaeger:4318"})
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://somewhere-else:4318")
    data = TestClient(R.app).get("/health").json()
    assert data["otel"]["endpoint"] == "http://jaeger:4318"
    assert data["otel"]["service_name"] == "svc-x"
