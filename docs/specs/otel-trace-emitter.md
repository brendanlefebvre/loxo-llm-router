# Spec: OpenTelemetry trace emitter

Status: draft · Track: observability · Target: v0.3 (observe-only, no routing change)

## Motivation

Loxo makes rich per-request decisions — local vs cloud, tier resolution, prompt-size
escalation, transparent fallback — but today they are visible only as `reason=` log
lines and two *aggregate* JSONL sinks (the spend ledger and the adequacy ledger).
There is no per-request waterfall. You cannot answer "why did *this* request go cloud,
and where did the 800 ms go?" from emitted, queryable data.

Emitting an [OpenTelemetry](https://opentelemetry.io/) trace per request turns each
request into a structured span tree that ships to *any* OTLP backend — Jaeger, Grafana
Tempo, Honeycomb — or to LangSmith, which ingests OTLP. It is the open standard, so the
same emitter feeds a fully-local Jaeger during development and a hosted backend for a
demo, with no code change.

This is **complementary to, not a replacement for, the ledgers**: the adequacy/spend
ledgers are append-only *aggregate operational history*; traces are the *per-request
distributed-tracing waterfall*. Same `Observation`, two sinks.

## Non-goals

- No new routing behavior. Observe-only, strictly off the critical path (same contract
  as A2 adequacy: "never affects routing or the bytes sent").
- Not a metrics/dashboard system — aggregation and charts are the backend's job.
- No proprietary LangSmith SDK. We emit vendor-neutral OTLP; LangSmith is merely one
  possible `OTEL_EXPORTER_OTLP_ENDPOINT`.
- No new hard dependency in the base install (see Config: lazy optional extra).

## The seam: reuse `Observation`

The work is already done by A2. `forward()` threads an `Observation` (`obs`) through
**both** the streaming and non-streaming paths, and populates it with everything a span
needs:

| `Observation` field | Span attribute (proposed) |
|---|---|
| `reason` | `loxo.reason` (`virtual-local`, `virtual-prompt-too-long`, `fallback`, …) |
| `route` | `loxo.route` (`local` \| `cloud`) |
| `served_model` | `gen_ai.response.model` |
| `status` | `http.response.status_code` |
| `ttfb_ms` | `loxo.ttfb_ms` (or a `first_byte` span event) |
| `latency_ms` | span duration |
| `finish_reason` | `gen_ai.response.finish_reasons` |
| `had_tool_calls` / `tool_calls_valid_json` | `loxo.tool_calls_valid_json` |
| `usage` | `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` |
| `usd` | `loxo.cost_usd` |
| `fallback_fired` | `loxo.fallback_fired` |

`obs` is already handed to `_spawn(ADEQUACY.write(obs))` at the three finalization sites
in `forward()` (the non-200 error path, the streaming-success tail, and the
non-streaming path), all fire-and-forget via `_spawn(...)`. **The trace emitter hooks the
same lifecycle**: emit a span built from `obs` at the same points.

Recommended refactor: introduce a single `record(obs)` choke point that fans out to both
sinks (`ADEQUACY.write(obs)` + `TRACES.emit(obs)`), so the three call sites collapse to
one and no future field-population site can forget a sink.

## Span shape (OTel GenAI semantic conventions)

Use the OTel **GenAI semconv** attribute names (`gen_ai.*`) rather than home-rolled keys —
that is the whole point: it is what LangSmith and every other backend already ingest.
Loxo-specific facts that have no semconv key get a `loxo.*` prefix.

- **Root span `loxo.chat_completion`** — one per inbound request. Starts at request entry,
  ends when `obs` is finalized. Attributes: `gen_ai.system=loxo`,
  `gen_ai.request.model` (the *requested* id, e.g. `loxo/auto`), plus the mapped fields
  from the table above. Span **status = ERROR** when `status != 200`.
- **Child span `loxo.upstream_call`** — the httpx POST to the resolved target.
- **Child span `loxo.fallback`** — emitted only when `fallback_fired`, so the waterfall
  visibly shows the local-attempt → cloud-retry hop. This hop is the single
  highest-value thing to see, so it is in scope for v1 even though the other child is
  optional.

## Config (opt-in, mirrors `SPEND_LEDGER` / `LOXO_CAPTURE_DIR`)

Follow the existing conventions exactly: unset/`""` disables, failures no-op, never break
a request.

- Enable on presence of `OTEL_EXPORTER_OTLP_ENDPOINT` (standard OTel var), or an explicit
  `LOXO_OTEL_ENABLED`. `""`/unset = fully off, zero overhead, zero spans.
- Honor the standard OTel env: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`
  (this is how LangSmith auth rides in), `OTEL_SERVICE_NAME` (default `loxo-llm-router`).
- **Fail-safe:** exporter init or export failure logs once and no-ops. Same contract the
  ledgers already hold ("a ledger read or write failure never breaks anything").
- **Lazy optional dependency:** `opentelemetry-sdk` + `opentelemetry-exporter-otlp` behind
  a `loxo-llm-router[otel]` extra in `pyproject.toml`, imported lazily so the base install
  gains no new hard dependency. If the extra is absent but tracing is requested, log the
  one-line remedy and no-op.

## Module

New `loxo_llm_router/tracing.py`, sibling to `ledger.py`: a `TraceEmitter` class with
`.emit(obs)`, constructed once at module load, gated on config, fail-safe — mirroring the
`SpendTracker` / `AdequacyLedger` shape (`TRACES = TraceEmitter(...)` module singleton).

## Test-first plan (mirrors `test_ledger.py` / `test_metadata.py`)

`test_tracing.py`, using OTel's `InMemorySpanExporter` (no network, deterministic):

- `test_disabled_by_default_no_spans` — no config → zero spans, no import cost paid.
- `test_local_request_emits_span_with_reason` — an `Observation(route=local,
  reason="virtual-local", …)` → one root span with the mapped attributes.
- `test_cloud_fallback_emits_fallback_attr` — `fallback_fired=True` → `loxo.fallback_fired`
  set and a `loxo.fallback` child span present.
- `test_error_status_sets_span_error` — `status=402` → span status ERROR.
- `test_usage_and_cost_attributes` — asserts the exact `gen_ai.usage.*` and `loxo.cost_usd`
  keys (guards against semconv drift).
- `test_export_failure_never_raises` — a deliberately broken exporter leaves the request
  path untouched.

## Task breakdown (SDD)

1. **Spec** — this document. *(done on this branch.)*
2. `tracing.py` + config gating + `test_tracing.py` RED → GREEN, pure in-memory exporter.
3. Wire the `record(obs)` choke point in `forward()` (collapse the three `obs`-write sites);
   add the `loxo.upstream_call` + `loxo.fallback` child spans.
4. `[otel]` extra in `pyproject.toml`; `docs/deploy.md` note + a Jaeger and a LangSmith
   `docker-compose`/env snippet for the demo.
5. `/health` gains an `otel` block (`enabled`, `endpoint`), mirroring the existing
   vision / spend blocks.

## Acceptance

With `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at a local Jaeger, a real OpenCode session on
`loxo/auto` produces one trace per request showing route, reason, tokens, cost, and
latency — **including the local → cloud fallback hop as a visible child span**. With the
endpoint unset: zero spans, zero overhead, no new hard dependency, suite green.

## Open questions (resolve at build time, do not hardcode from memory)

- Confirm LangSmith's current OTLP ingest endpoint and whether it expects the API key via
  `OTEL_EXPORTER_OTLP_HEADERS` — verify against live LangSmith docs when task 4 lands.
- The GenAI semconv is still evolving; pin to the version shipped and record it in a
  comment so a later bump is a deliberate edit, not silent drift.
- v1 span depth: root-only is cheapest, root + `fallback` child is the recommended v1
  (the fallback hop earns its span); the full `upstream_call` child can follow.
