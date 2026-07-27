"""OpenTelemetry trace emitter (observe-only, Plan B).

Emits one root span per finalized request, built retroactively from the
Observation at the record() choke point — i.e. only after the response is
finalized, so it cannot affect routing or the bytes sent. Handoff to the
BatchSpanProcessor queue is non-blocking; export happens on its own thread.
OpenTelemetry is a lazy optional
dependency (the [otel] extra); absent or unconfigured, this is a no-op.

Attribute names follow the OTel GenAI semantic conventions (gen_ai.*); loxo
facts with no semconv key use a loxo.* prefix.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .ledger import Observation

_TRUTHY = {"1", "true", "yes", "on"}


def resolve_otel_config() -> dict[str, Any] | None:
    """Tracing config when enabled, else None (fully off, zero spans).

    Enabled iff either standard OTLP endpoint var is set non-empty, or
    LOXO_OTEL_ENABLED is truthy. The exporter reads endpoint/headers from the
    standard OTEL_* env (so OTEL_EXPORTER_OTLP_HEADERS carries e.g. LangSmith
    auth for free); this only decides on/off and the service name. The endpoint
    is captured here so /health can report the value actually in force, not a
    later env edit that has not been picked up (config resolves once, at import).

    Both endpoint vars must be honored, in the exporter's own precedence: the
    signal-specific OTEL_EXPORTER_OTLP_TRACES_ENDPOINT wins over the generic
    OTEL_EXPORTER_OTLP_ENDPOINT, and is used verbatim (the generic one gets
    /v1/traces appended). Gating on the generic var alone would silently
    disable tracing for a deployment that set only the signal-specific one —
    an entirely valid OTel config the exporter would otherwise have honored.
    """
    endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
                or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())
    enabled = os.environ.get("LOXO_OTEL_ENABLED", "").strip().lower() in _TRUTHY
    if not endpoint and not enabled:
        return None
    return {"service_name": os.environ.get("OTEL_SERVICE_NAME", "loxo-llm-router").strip()
            or "loxo-llm-router",
            "endpoint": endpoint or None}


class TraceEmitter:
    """Fan a finalized Observation out to OTLP as one span tree. Fail-safe:
    any init or emit failure logs once and no-ops."""

    def __init__(self, tracer: Any = None,
                 log: Callable[[str], None] = lambda _m: None,
                 endpoint: str | None = None):
        self._tracer = tracer
        self._log = log
        self.enabled = tracer is not None
        # Where spans actually go, straight from the built exporter. With
        # LOXO_OTEL_ENABLED and no endpoint var, the exporter falls back to its
        # own default; reporting the unset config would under-report /health.
        self.endpoint = endpoint

    @classmethod
    def from_config(cls, config: dict[str, Any] | None,
                    log: Callable[[str], None] = lambda _m: None) -> "TraceEmitter":
        if config is None:
            return cls(tracer=None, log=log)
        try:
            tracer, endpoint = _build_otlp_tracer(config)
        except Exception as e:  # noqa: BLE001 - tracing must never break startup
            log(f"[router] OTel tracing requested but init failed ({e}); "
                f"tracing disabled. Install loxo-llm-router[otel].")
            return cls(tracer=None, log=log, endpoint=config.get("endpoint"))
        return cls(tracer=tracer, log=log, endpoint=endpoint)

    def emit(self, obs: "Observation") -> None:
        if not self.enabled or self._tracer is None:
            return
        try:
            self._emit(obs)
        except Exception as e:  # noqa: BLE001 - observe-only: never break a response
            self._log(f"[router] OTel span emit failed ({e}); span dropped")

    def _emit(self, obs: "Observation") -> None:
        from opentelemetry.trace import Status, StatusCode

        # Wall-clock end; latency_ms was monotonic in forward(). Sub-ms drift,
        # acceptable for retroactive span timing (child uses the same arithmetic).
        end_ns = time.time_ns()
        dur_ns = int((obs.latency_ms or 0) * 1_000_000)
        start_ns = end_ns - dur_ns

        span = self._tracer.start_span("loxo.chat_completion", start_time=start_ns)
        try:
            _set(span, "gen_ai.system", "loxo")
            _set(span, "gen_ai.request.model", obs.requested_model)
            _set(span, "gen_ai.response.model", obs.served_model)
            _set(span, "loxo.route", obs.route)
            _set(span, "loxo.reason", obs.reason)
            _set(span, "http.response.status_code", obs.status)
            _set(span, "loxo.ttfb_ms", obs.ttfb_ms)
            _set(span, "loxo.cost_usd", obs.usd)
            _set(span, "loxo.fallback_fired", obs.fallback_fired)
            _set(span, "loxo.tool_calls_valid_json", obs.tool_calls_valid_json)
            _set(span, "loxo.session_id", obs.session_id)
            if obs.finish_reason is not None:
                _set(span, "gen_ai.response.finish_reasons", [obs.finish_reason])
            usage = obs.usage or {}
            _set(span, "gen_ai.usage.input_tokens", usage.get("prompt_tokens"))
            _set(span, "gen_ai.usage.output_tokens", usage.get("completion_tokens"))
            if obs.status is not None and obs.status != 200:
                span.set_status(Status(StatusCode.ERROR))

            if obs.fallback_fired and obs.fallback_at_ms is not None:
                from opentelemetry.trace import set_span_in_context
                fb_start = start_ns + int(obs.fallback_at_ms * 1_000_000)
                ctx = set_span_in_context(span)
                child = self._tracer.start_span(
                    "loxo.fallback", context=ctx, start_time=fb_start)
                child.end(end_time=end_ns)
        finally:
            span.end(end_time=end_ns)


def _set(span: Any, key: str, value: Any) -> None:
    """Set an attribute, skipping None (OTel attributes cannot be None; a None
    here means 'omit the attribute entirely')."""
    if value is not None:
        span.set_attribute(key, value)


def _build_otlp_tracer(config: dict[str, Any]) -> tuple[Any, str | None]:
    """Construct a TracerProvider wired to the OTLP/HTTP exporter, and report
    the URL it resolved to. Lazy imports keep OpenTelemetry out of the base
    install. The exporter reads OTEL_EXPORTER_OTLP[_TRACES]_ENDPOINT / _HEADERS
    from the environment itself."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create({"service.name": config["service_name"]})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    # getattr, not attribute access: this is a private attr and a display field
    # only — an SDK rename must not take tracing down with it.
    resolved = getattr(exporter, "_endpoint", None) or config.get("endpoint")
    return provider.get_tracer("loxo-llm-router"), resolved
