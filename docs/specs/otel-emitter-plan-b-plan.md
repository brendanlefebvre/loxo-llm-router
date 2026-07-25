# OTel Trace Emitter — Plan B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit one OpenTelemetry trace per `/v1/chat/completions` request — a root span built from the finalized `Observation`, plus a `loxo.fallback` child span when a local→cloud transport fallback fired — exportable to any OTLP backend, off by default with zero overhead.

**Architecture:** The emitter is the *second sink* on the `record(obs)` choke point Plan A built. It runs **once, at finalization** (never on the live request path): `TRACES.emit(obs)` reconstructs the span tree retroactively from `obs`, using recorded elapsed-millisecond markers so span times are real. No span-context threading through `forward()` — this is the "Option A" span-depth decision (see below). OTel is a lazy optional dependency behind a `[otel]` extra; absent or unconfigured, the emitter is a no-op.

**Tech Stack:** Python 3.11+, FastAPI, `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http` (optional extra), OTLP/HTTP export honoring standard `OTEL_*` env vars.

**Parent spec:** [otel-trace-emitter.md](otel-trace-emitter.md). This plan implements its tasks 3–6; tasks 1–2 (spec + `record(obs)` choke point + observe-only `session_id`) shipped as Plan A in PR #18 (commit `95c3199`).

## Design decisions resolved (before this plan)

- **Span depth = Option A (retroactive from `obs`).** The emitter builds spans *after* the request finishes, from `obs`, at the `record()` seam. Chosen over live instrumentation (Option C) for time-to-ship and because it keeps the observe-only/off-critical-path contract for free — the emitter cannot run until the request is already done, so a span bug can never affect the bytes sent. Option C (live child spans around the httpx calls, with span-context threading through `forward()` + `streamer()`) stays available later as a purely *additive* change; nothing here blocks it.
- **One new `Observation` field: `fallback_at_ms`.** The fallback child span needs to know *when* the local attempt gave way to the cloud retry — not derivable from total `latency_ms`. `forward()` records this single elapsed-ms marker at the two points it already knows it. The `loxo.fallback` child span is then `[fallback_at_ms, latency_ms]`; the root is `[0, latency_ms]`.
- **`fallback_at_ms` is Observation-only — NOT added to `to_entry()`.** It is a tracing concern, not adequacy-ledger material (the ledger already carries `latency_ms`). Keeping it out of `to_entry()` leaves the pinned adequacy schema (`test_entry_has_exact_spec_schema`) untouched. `Observation` is a shared carrier; not every field must serialize to every sink.
- **The optional `loxo.upstream_call` child span is deferred.** The parent spec marks it "can follow"; the fallback hop is the high-value one. Out of scope for this plan.
- **Testability via injected tracer.** `TraceEmitter(tracer=...)` takes a ready tracer so tests drive it with an `InMemorySpanExporter` + `SimpleSpanProcessor` (deterministic, no network). Production builds its tracer via `TraceEmitter.from_config(...)`.

## Global Constraints

- **Python ≥ 3.11**, matching `requires-python`. CI matrix: 3.11, 3.12, 3.13.
- **No new hard dependency in the base install.** All `opentelemetry` imports are lazy (inside functions), behind the `[otel]` extra. Base `pip install -e .` gains nothing.
- **Observe-only, strictly off the critical path.** Tracing never influences routing, target selection, or the bytes sent. Any exporter/init/emit failure logs once and no-ops — same contract the ledgers hold.
- **Disabled by default.** Unset/`""` config = fully off, zero spans, no OTel import cost paid.
- **OTel GenAI semconv attribute names** (`gen_ai.*`); loxo-specific facts get a `loxo.*` prefix. Pin the semconv assumptions in a comment so a later bump is deliberate.
- **Config mirrors existing conventions** (`SPEND_LEDGER` / adequacy): env-driven, failures no-op, never break a request.
- Repo test convention: flat `test_*.py` in the repo root; async via `asyncio.run(...)`; `import loxo_llm_router as R`; FastAPI `TestClient`; `conftest.py` disables ledgers and isolates config.
- This spec/plan is a branch-only working doc: committed with `git add -f`, `git rm`'d before merge to `main` (per repo convention). The durable docs it touches — `docs/deploy.md`, `/health` — stay on `main`.

---

### Task 1: `[otel]` optional extra + CI wiring

Sets up the dependency so later tasks' tests can import OpenTelemetry, and so CI actually runs them. Thin but foundational — everything downstream imports `opentelemetry` in tests.

**Files:**
- Modify: `pyproject.toml` (add `[project.optional-dependencies]`)
- Modify: `.github/workflows/ci.yml` (install the extra)

**Interfaces:**
- Produces: an installable `loxo-llm-router[otel]` extra providing `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http`.

- [ ] **Step 1: Add the extra to `pyproject.toml`**

Insert after the `dependencies = [...]` block (currently ends at the line with `"httpx>=0.27",` and its closing `]`):

```toml
[project.optional-dependencies]
# OpenTelemetry trace export (observe-only). Lazy-imported; the base install
# needs none of this. Semconv GenAI attributes assumed at the 1.x line shipped
# below — bump deliberately, not silently.
otel = [
  "opentelemetry-sdk>=1.20",
  "opentelemetry-exporter-otlp-proto-http>=1.20",
]
```

- [ ] **Step 2: Update CI to install the extra**

In `.github/workflows/ci.yml`, change the install step from:

```yaml
      - name: Install
        run: pip install -e . pytest
```

to:

```yaml
      - name: Install
        run: pip install -e .[otel] pytest
```

- [ ] **Step 3: Verify the extra resolves and imports locally**

Run:
```bash
pip install -e .[otel]
python -c "import opentelemetry.sdk.trace; import opentelemetry.exporter.otlp.proto.http.trace_exporter; print('otel ok')"
```
Expected: `otel ok` (no ImportError).

- [ ] **Step 4: Confirm the base suite still passes**

Run: `python3 -m pytest -q`
Expected: PASS (136 passed — unchanged; no code touched yet).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml
git commit -m "build: add [otel] optional extra; CI installs it"
```

---

### Task 2: `TraceEmitter` — root span from `obs`

The core emitter: a new module that turns a finalized `Observation` into one OTel root span with the mapped attributes. No fallback child yet, no `forward()`/`record()` wiring yet — fully unit-testable with fabricated observations.

**Files:**
- Create: `loxo_llm_router/tracing.py`
- Test: `test_tracing.py`

**Interfaces:**
- Consumes: `loxo_llm_router.ledger.Observation` (fields: `requested_model`, `served_model`, `route`, `reason`, `status`, `ttfb_ms`, `latency_ms`, `finish_reason`, `tool_calls_valid_json`, `usage`, `usd`, `fallback_fired`, `session_id`).
- Produces:
  - `resolve_otel_config() -> dict | None` — `{"service_name": str}` when enabled, else `None`.
  - `class TraceEmitter` with `__init__(self, tracer=None, log=<callable>)`, attribute `enabled: bool`, classmethod `from_config(cls, config: dict | None, log=<callable>) -> TraceEmitter`, and `emit(self, obs: Observation) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `test_tracing.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_tracing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'loxo_llm_router.tracing'`.

- [ ] **Step 3: Write `tracing.py`**

Create `loxo_llm_router/tracing.py`:

```python
"""OpenTelemetry trace emitter (observe-only, Plan B).

Emits one root span per finalized request, built retroactively from the
Observation at the record() choke point. Never runs on the live request path,
so it cannot affect routing or the bytes sent. OpenTelemetry is a lazy optional
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

    Enabled iff OTEL_EXPORTER_OTLP_ENDPOINT is set non-empty, or LOXO_OTEL_ENABLED
    is truthy. The exporter reads endpoint/headers from the standard OTEL_* env
    (so OTEL_EXPORTER_OTLP_HEADERS carries e.g. LangSmith auth for free); this
    only decides on/off and the service name.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    enabled = os.environ.get("LOXO_OTEL_ENABLED", "").strip().lower() in _TRUTHY
    if not endpoint and not enabled:
        return None
    return {"service_name": os.environ.get("OTEL_SERVICE_NAME", "loxo-llm-router").strip()
            or "loxo-llm-router"}


class TraceEmitter:
    """Fan a finalized Observation out to OTLP as one span tree. Fail-safe:
    any init or emit failure logs once and no-ops."""

    def __init__(self, tracer: Any = None,
                 log: Callable[[str], None] = lambda _m: None):
        self._tracer = tracer
        self._log = log
        self.enabled = tracer is not None

    @classmethod
    def from_config(cls, config: dict[str, Any] | None,
                    log: Callable[[str], None] = lambda _m: None) -> "TraceEmitter":
        if config is None:
            return cls(tracer=None, log=log)
        try:
            tracer = _build_otlp_tracer(config)
        except Exception as e:  # noqa: BLE001 - tracing must never break startup
            log(f"[router] OTel tracing requested but init failed ({e}); "
                f"tracing disabled. Install loxo-llm-router[otel].")
            return cls(tracer=None, log=log)
        return cls(tracer=tracer, log=log)

    def emit(self, obs: "Observation") -> None:
        if not self.enabled or self._tracer is None:
            return
        try:
            self._emit(obs)
        except Exception as e:  # noqa: BLE001 - observe-only: never break a response
            self._log(f"[router] OTel span emit failed ({e}); span dropped")

    def _emit(self, obs: "Observation") -> None:
        from opentelemetry.trace import Status, StatusCode

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
        finally:
            span.end(end_time=end_ns)


def _set(span: Any, key: str, value: Any) -> None:
    """Set an attribute, skipping None (OTel attributes cannot be None; a None
    here means 'omit the attribute entirely')."""
    if value is not None:
        span.set_attribute(key, value)


def _build_otlp_tracer(config: dict[str, Any]) -> Any:
    """Construct a TracerProvider wired to the OTLP/HTTP exporter. Lazy imports
    keep OpenTelemetry out of the base install. The exporter reads
    OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS from the environment itself."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create({"service.name": config["service_name"]})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider.get_tracer("loxo-llm-router")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_tracing.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add loxo_llm_router/tracing.py test_tracing.py
git commit -m "feat: TraceEmitter root span from Observation (observe-only)"
```

---

### Task 3: `loxo.fallback` child span + `fallback_at_ms` field

Add the observe-only `fallback_at_ms` field to `Observation`, and the fallback child span to `emit()`. Still pure unit test — a fabricated fallback observation, no `forward()` needed.

**Files:**
- Modify: `loxo_llm_router/ledger.py` (add field to `Observation`, after `session_id`)
- Modify: `loxo_llm_router/tracing.py` (`_emit` — add child span)
- Test: `test_tracing.py` (add fallback tests)

**Interfaces:**
- Consumes: `Observation.fallback_fired: bool`, new `Observation.fallback_at_ms: int | None`.
- Produces: a child span named `loxo.fallback`, parented to the root, spanning `[fallback_at_ms, latency_ms]`, emitted only when `fallback_fired and fallback_at_ms is not None`.

- [ ] **Step 1: Write the failing tests**

Add to `test_tracing.py`:

```python
def test_fallback_emits_child_span():
    em, exporter = _emitter_and_exporter()
    em.emit(_obs(route="cloud", reason="fallback", fallback_fired=True,
                 fallback_at_ms=100, latency_ms=800))
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert names == {"loxo.chat_completion", "loxo.fallback"}
    root = next(s for s in spans if s.name == "loxo.chat_completion")
    child = next(s for s in spans if s.name == "loxo.fallback")
    assert root.attributes["loxo.fallback_fired"] is True
    # child is parented to root and nested within its window
    assert child.parent is not None
    assert child.parent.span_id == root.context.span_id
    assert root.start_time <= child.start_time
    assert child.end_time <= root.end_time
    # child begins ~fallback_at_ms into the root's 800ms window
    assert child.start_time - root.start_time == 100 * 1_000_000


def test_no_fallback_child_when_not_fired():
    em, exporter = _emitter_and_exporter()
    em.emit(_obs(fallback_fired=False, fallback_at_ms=None))
    assert {s.name for s in exporter.get_finished_spans()} == {"loxo.chat_completion"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_tracing.py::test_fallback_emits_child_span -v`
Expected: FAIL — `TypeError` on unexpected `fallback_at_ms` kwarg (field not defined yet).

- [ ] **Step 3: Add the `Observation` field**

In `loxo_llm_router/ledger.py`, add after `session_id: str | None = None` (currently the last field, line ~217):

```python
    fallback_at_ms: int | None = None  # tracing-only: elapsed-ms when local->cloud retry began; not serialized to the ledger
```

(Do NOT touch `to_entry()` — this field is intentionally not in the adequacy schema.)

- [ ] **Step 4: Add the child span to `_emit`**

In `loxo_llm_router/tracing.py`, replace the `finally:` block at the end of `_emit` with logic that emits the child *before* ending the root:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest test_tracing.py -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Commit**

```bash
git add loxo_llm_router/ledger.py loxo_llm_router/tracing.py test_tracing.py
git commit -m "feat: loxo.fallback child span from obs.fallback_at_ms"
```

---

### Task 4: populate `fallback_at_ms` in `forward()`

Record the elapsed-ms marker at the two points where `forward()` pivots from the local attempt to the cloud retry — the streaming pre-first-byte fallback and the non-streaming fallback. This makes real requests carry the split time the child span reads.

**Files:**
- Modify: `loxo_llm_router/__init__.py` (`forward()` — streaming fallback at `:808`, non-streaming fallback at `:897`)
- Test: `test_tracing_fallback_timing.py`

**Interfaces:**
- Consumes: the module-local `_t0` (request-start monotonic time) already present in `forward()`.
- Produces: `obs.fallback_at_ms` set to `int((loop.time() - _t0) * 1000)` at both fallback pivots.

- [ ] **Step 1: Write the failing test**

Create `test_tracing_fallback_timing.py`:

```python
"""forward() records obs.fallback_at_ms when a transport fallback fires."""

import asyncio
import json

import httpx
import loxo_llm_router as R
from loxo_llm_router import ledger


class _Resp:
    def __init__(self, status=200, content=b'{"choices":[]}'):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": "application/json"}


def test_nonstreaming_fallback_sets_fallback_at_ms(monkeypatch):
    # First POST (local) raises a transport error; the retry (cloud) succeeds.
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("local down")
            return _Resp()

    monkeypatch.setattr(R.httpx, "AsyncClient", FakeClient)
    obs = ledger.Observation(cls="main", classifier_version=1,
                             requested_model="loxo/auto", route="local",
                             served_model="qwen3-30b", reason="virtual-local",
                             stream=False)
    monkeypatch.setattr(R, "record", lambda o: None)  # isolate: don't hit sinks

    asyncio.run(R.forward(
        primary_url=R.LOCAL_BASE_URL, path="/chat/completions",
        primary_body=json.dumps({"model": "x"}).encode(),
        client_headers={}, stream=False,
        fallback_url=R.CLOUD_BASE_URL, fallback_body=json.dumps({"model": "y"}).encode(),
        cloud_model=None, reason="virtual-local", obs=obs))

    assert obs.fallback_fired is True
    assert obs.fallback_at_ms is not None
    assert obs.fallback_at_ms >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_tracing_fallback_timing.py -v`
Expected: FAIL — `assert obs.fallback_at_ms is not None` (field stays `None`; not set yet).

- [ ] **Step 3: Set `fallback_at_ms` at the streaming fallback pivot**

In `loxo_llm_router/__init__.py`, inside the streaming `except FALLBACK_ERRORS:` block (around `:808`), where `served_reason = "fallback"` is assigned, add the marker right after it:

```python
                    served_reason = "fallback"
                    if obs is not None:
                        obs.fallback_at_ms = int((asyncio.get_event_loop().time() - _t0) * 1000)
                    can_fallback = False
                    continue
```

- [ ] **Step 4: Set `fallback_at_ms` at the non-streaming fallback pivot**

In the non-streaming `except FALLBACK_ERRORS:` block (around `:897`), after `served_reason = "fallback"`:

```python
            served_cloud_model = fb_model
            served_cloud_provider = _provider_host(CLOUD_BASE_URL)
            served_reason = "fallback"
            if obs is not None:
                obs.fallback_at_ms = int((asyncio.get_event_loop().time() - _t0) * 1000)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest test_tracing_fallback_timing.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite (guard the refactor)**

Run: `python3 -m pytest -q`
Expected: PASS (all prior tests still green; new ones added).

- [ ] **Step 7: Commit**

```bash
git add loxo_llm_router/__init__.py test_tracing_fallback_timing.py
git commit -m "feat: record obs.fallback_at_ms at both forward() fallback pivots"
```

---

### Task 5: wire `TRACES` into `record()`

Construct the module singleton next to `SPEND`/`ADEQUACY` and add the emit call to the `record()` fan-out — the seam Plan A left as a comment.

**Files:**
- Modify: `loxo_llm_router/__init__.py` (import, singleton near `:371`, `record()` at `:409`)
- Test: `test_tracing_wiring.py`

**Interfaces:**
- Consumes: `resolve_otel_config`, `TraceEmitter` from `loxo_llm_router.tracing`; the existing `log` callable and `record(obs)` choke point.
- Produces: module globals `_OTEL_CFG: dict | None` and `TRACES: TraceEmitter`; `record()` now calls `TRACES.emit(obs)`.

- [ ] **Step 1: Write the failing test**

Create `test_tracing_wiring.py`:

```python
"""record() fans a finalized obs out to the trace emitter."""

import asyncio

import loxo_llm_router as R
from loxo_llm_router import ledger


def _obs():
    return ledger.Observation(cls="main", classifier_version=1,
                              requested_model="loxo/auto", route="local",
                              served_model="qwen3-30b", reason="virtual-local",
                              stream=False, latency_ms=10)


def test_record_calls_trace_emit(monkeypatch):
    seen = []
    monkeypatch.setattr(R.TRACES, "emit", lambda o: seen.append(o))

    async def fake_write(obs):  # keep the adequacy sink quiet
        return None

    monkeypatch.setattr(R.ADEQUACY, "write", fake_write)
    o = _obs()

    async def drive():
        R.record(o)  # _spawn needs a running loop; TRACES.emit is sync (fires now)
        await asyncio.gather(*list(R._BACKGROUND_TASKS))

    asyncio.run(drive())
    assert seen == [o]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_tracing_wiring.py -v`
Expected: FAIL — `AttributeError: module 'loxo_llm_router' has no attribute 'TRACES'`.

- [ ] **Step 3: Add the import**

In `loxo_llm_router/__init__.py`, extend the ledger/tracing imports near the top (after the `from .ledger import (...)` block, ~`:115`):

```python
from .tracing import TraceEmitter, resolve_otel_config
```

- [ ] **Step 4: Construct the singleton**

After `ADEQUACY = AdequacyLedger(resolve_adequacy_ledger(), log=log)` (~`:371`):

```python
_OTEL_CFG = resolve_otel_config()
TRACES = TraceEmitter.from_config(_OTEL_CFG, log=log)
```

- [ ] **Step 5: Add the emit to `record()`**

In `record()` (~`:409`), replace the seam comment with the real call:

```python
def record(obs: Observation) -> None:
    """Single finalization choke point: fan a completed obs out to every sink.

    Synchronous — only schedules fire-and-forget work, so it is safe from the
    streaming error path, the streamer() generator, and the non-streaming path.
    """
    _spawn(ADEQUACY.write(obs))
    TRACES.emit(obs)  # observe-only; no-op unless OTel is configured
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest test_tracing_wiring.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add loxo_llm_router/__init__.py test_tracing_wiring.py
git commit -m "feat: wire TRACES.emit into the record() fan-out"
```

---

### Task 6: `/health` otel block + deploy docs

Surface tracing status on `/health` (mirroring the `vision`/`spend` blocks), and document how to point it at a local Jaeger or LangSmith.

**Files:**
- Modify: `loxo_llm_router/__init__.py` (`health()` at `:1198`)
- Create or modify: `docs/deploy.md`
- Test: `test_health_otel.py`

**Interfaces:**
- Consumes: module globals `TRACES`, `_OTEL_CFG`; standard `OTEL_EXPORTER_OTLP_ENDPOINT` env.
- Produces: an `"otel"` block in the `/health` JSON: `{enabled, endpoint, service_name}`.

- [ ] **Step 1: Write the failing test**

Create `test_health_otel.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_health_otel.py -v`
Expected: FAIL — `assert "otel" in data` (block absent).

- [ ] **Step 3: Add the otel block to `health()`**

In `loxo_llm_router/__init__.py`, in the `health()` return dict (after the `"spend": spend_summary,` line, ~`:1220`):

```python
        "otel": {
            "enabled": TRACES.enabled,
            "endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            "service_name": (_OTEL_CFG or {}).get("service_name"),
        },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_health_otel.py -v`
Expected: PASS.

- [ ] **Step 5: Write the deploy docs**

Create `docs/deploy.md` (or append this section if it already exists):

````markdown
## OpenTelemetry tracing (optional)

Loxo can emit one OTLP trace per request — route, reason, tokens, cost, latency,
and the local→cloud fallback hop as a child span. It is **off by default** and
strictly observe-only: enabling it never changes routing or the response.

Install the extra:

```bash
pip install 'loxo-llm-router[otel]'
```

Enable by pointing at any OTLP/HTTP endpoint (standard OpenTelemetry env vars):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=loxo-llm-router          # optional; this is the default
```

`GET /health` shows the live status under `"otel"` (`enabled`, `endpoint`,
`service_name`). Unset the endpoint (and `LOXO_OTEL_ENABLED`) for zero spans and
zero overhead.

### Local Jaeger (all-in-one)

```yaml
# docker-compose.yml
services:
  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"   # UI
      - "4318:4318"     # OTLP/HTTP
```

```bash
docker compose up -d
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# run loxo, send a request, then open http://localhost:16686
```

### LangSmith (OTLP ingest)

LangSmith ingests OTLP directly — no proprietary SDK. Auth rides in via the
standard headers env var:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.smith.langchain.com/otel
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=<LANGSMITH_API_KEY>"
```

> Verify LangSmith's current OTLP endpoint path and header name against their
> live docs before relying on this — both have changed historically.
````

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add loxo_llm_router/__init__.py docs/deploy.md test_health_otel.py
git commit -m "feat: /health otel block; docs: OTel deploy (Jaeger + LangSmith)"
```

---

## Acceptance

With `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at a local Jaeger, a real request on
`loxo/auto` produces one trace: a `loxo.chat_completion` root span carrying
route, reason, tokens, cost, and latency, plus a `loxo.fallback` child span with
real timing whenever the local→cloud transport fallback fired. With the endpoint
unset: zero spans, zero overhead, no new hard dependency, suite green.

## Self-review

**Spec coverage (parent tasks 3–6):**
- Task 3 (`tracing.py` + config gating + in-memory-exporter tests) → plan Task 2 + Task 3. ✓
- Task 4 (`TRACES.emit` on the `record()` fan-out; child spans) → plan Task 5 (wiring) + Task 3/4 (fallback child + its timing). The optional `loxo.upstream_call` child is explicitly deferred (documented above; parent spec marks it "can follow"). ✓ (with the noted, deliberate deferral)
- Task 5 (`[otel]` extra + deploy docs) → plan Task 1 (extra + CI) + Task 6 (deploy docs). ✓
- Task 6 (`/health` otel block) → plan Task 6. ✓
- Span-shape attribute table (parent spec) → mapped in Task 2's `_emit`; `loxo.session_id` omitted-when-None covered by `test_session_id_attribute_present_and_omitted`. ✓
- Config fail-safe (init/export failure logs once, no-ops) → `from_config` try/except + `emit` try/except; `test_export_failure_never_raises`. ✓
- Disabled-by-default / no import cost → lazy imports + `test_disabled_by_default_no_spans`. ✓

**Deliberate scope calls (not gaps):** `loxo.upstream_call` child span deferred; span depth is Option A (retroactive), not live instrumentation — both decided above with rationale.

**Type consistency:** `TraceEmitter(tracer=…)` and `TraceEmitter.from_config(config, log=…)` used identically in tests and in the `__init__.py` singleton. `resolve_otel_config() -> dict | None` returns `{"service_name": …}`, read the same way in the singleton and `/health`. `Observation.fallback_at_ms: int | None` defined in Task 3, set in Task 4, read in Task 3's `_emit`. `record(obs)` signature unchanged.

**Placeholder scan:** no TBD/TODO; every code and test step carries concrete content.
