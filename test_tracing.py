"""Tests for the OTel trace emitter (Plan B). Uses an in-memory exporter —
no network, deterministic."""

import pytest

pytest.importorskip("opentelemetry")  # skip cleanly when the [otel] extra is absent

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from loxo_llm_router import tracing
from loxo_llm_router import ledger


def _obs(**kw):
    base = dict(cls="main", classifier_version=1, requested_model="loxo/auto",
                route="local", served_model="qwen3-30b", reason="virtual-local",
                stream=False, status=200, latency_ms=800, ttfb_ms=120,
                usage={"prompt_tokens": 100, "completion_tokens": 20}, usd=0.0)
    base.update(kw)
    return ledger.Observation(**base)


def _emitter_and_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    em = tracing.TraceEmitter(tracer=provider.get_tracer("test"))
    return em, exporter


def test_disabled_by_default_no_spans():
    em = tracing.TraceEmitter.from_config(None)
    assert em.enabled is False
    em.emit(_obs())  # must not raise, must produce nothing observable


def test_local_request_emits_root_span_with_attributes():
    em, exporter = _emitter_and_exporter()
    em.emit(_obs(route="local", reason="virtual-local", served_model="qwen3-30b"))
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "loxo.chat_completion"
    assert s.attributes["gen_ai.system"] == "loxo"
    assert s.attributes["gen_ai.request.model"] == "loxo/auto"
    assert s.attributes["gen_ai.response.model"] == "qwen3-30b"
    assert s.attributes["loxo.route"] == "local"
    assert s.attributes["loxo.reason"] == "virtual-local"
    assert s.attributes["http.response.status_code"] == 200


def test_session_id_attribute_present_and_omitted():
    em, exporter = _emitter_and_exporter()
    em.emit(_obs(session_id="sess-1"))
    em.emit(_obs(session_id=None))
    spans = exporter.get_finished_spans()
    assert spans[0].attributes["loxo.session_id"] == "sess-1"
    assert "loxo.session_id" not in spans[1].attributes  # None => omitted, never null


def test_error_status_sets_span_error():
    from opentelemetry.trace import StatusCode
    em, exporter = _emitter_and_exporter()
    em.emit(_obs(status=402))
    s = exporter.get_finished_spans()[0]
    assert s.status.status_code == StatusCode.ERROR
    assert s.attributes["http.response.status_code"] == 402


def test_usage_and_cost_attributes():
    em, exporter = _emitter_and_exporter()
    em.emit(_obs(usage={"prompt_tokens": 100, "completion_tokens": 20}, usd=0.0123))
    s = exporter.get_finished_spans()[0]
    assert s.attributes["gen_ai.usage.input_tokens"] == 100
    assert s.attributes["gen_ai.usage.output_tokens"] == 20
    assert s.attributes["loxo.cost_usd"] == 0.0123


def test_export_failure_never_raises():
    class BoomTracer:
        def start_span(self, *a, **k):
            raise RuntimeError("exporter is on fire")
    em = tracing.TraceEmitter(tracer=BoomTracer())
    em.emit(_obs())  # must swallow the error, not propagate
