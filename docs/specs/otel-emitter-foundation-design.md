# Design: OTel emitter foundation (Plan A)

Status: draft · Track: observability · Target: v0.3 · Parent spec: [otel-trace-emitter.md](otel-trace-emitter.md)

This is **Plan A of two.** It builds the parts of the OTel trace emitter that carry
their own weight *before* any OpenTelemetry code exists: the `record(obs)` finalization
choke point and the `loxo.session_id` groundwork. Plan B (the emitter proper —
`tracing.py`, spans, the `[otel]` extra, the `/health` block, deploy docs) builds on top
of this seam.

The slice is deliberate. Both pieces here are either a pure refactor or an observe-only
field that lands in an existing sink, so Plan A has **no OpenTelemetry dependency**, is
mergeable to `main` on its own, and de-risks the seam Plan B hooks. It maps to the parent
spec's task 2 (the choke point, promoted to "lands before any tracing code") plus the
`session_id` groundwork from its Span-shape section.

## Scope

In scope:

1. A single `record(obs)` choke point that collapses the three finalization sites in
   `forward()`. Pure refactor; today it fans out to the adequacy ledger alone.
2. A `session_id: str | None` field on `Observation`, resolved once per request by a pure
   stateless helper, persisted to the adequacy ledger via `to_entry()`.
3. Tests for both, plus the existing adequacy suite as the refactor's safety net.

Out of scope — all Plan B: `tracing.py`, any span, the `loxo.session_id` *span attribute*,
the `[otel]` pyproject extra, the `/health` otel block, and deploy docs. `record()` ships
with the `TRACES.emit(obs)` seam present only as a comment.

## Component 1 — the `record(obs)` choke point

Today `forward()` (`loxo_llm_router/__init__.py`) finalizes an `Observation` at three
sites, each ending in the identical line `_spawn(ADEQUACY.write(obs))`:

- `__init__.py:796` — streaming non-200 error path (before returning `JSONResponse`).
- `__init__.py:828` — streaming-success tail, inside the `streamer()` async generator.
- `__init__.py:888` — non-streaming path.

Introduce one function, sited next to the `SPEND` / `ADEQUACY` singletons (~`__init__.py:365`):

```python
def record(obs: Observation) -> None:
    """Single finalization choke point: fan a completed obs out to every sink.
    Fire-and-forget; never awaited on the request path.
    Plan B (OTel) adds the second sink at THIS one site, not three."""
    _spawn(ADEQUACY.write(obs))
    # Plan B: _spawn(TRACES.emit(obs))  — the emitter hooks here.
```

Replace each of the three `_spawn(ADEQUACY.write(obs))` lines with `record(obs)`.

`record()` is **synchronous** and works in all three contexts because it only *schedules*
fire-and-forget work via `_spawn` (`asyncio.ensure_future`, `__init__.py:353`); every site
runs inside the request coroutine with a live event loop, including the `streamer()`
generator.

**This is a pure refactor with zero behavior change.** The existing adequacy tests
(`test_adequacy.py`) are the safety net and must stay green unchanged — the same
observations must still reach the ledger from all three paths.

Rationale (from the parent spec): the choke point exists so a new sink is wired **once, not
three times**. The sinks that consume a finalized `Observation` are:

1. the **adequacy ledger** (`ADEQUACY.write`) — the only concrete sink today, and the sole
   one Plan A fans to;
2. the **OTel trace emitter** (`TRACES.emit`) — the *second* sink, added at this seam in
   Plan B;
3. a **plausible third** — not defined or committed. The nearest named candidate is the
   parent spec's possible-v0.4 agent-readable session/trajectory sink. This entry exists
   only to justify the choke point, not to promise a specific sink.

The **spend ledger** (`SPEND.record`) is deliberately *not* one of these: it is fed
separately, at its own points in `forward()` for cloud cost attribution, and does not ride
the `record(obs)` fan-out — the choke point is specifically for sinks that consume a
finalized `Observation`. Every future obs-sink wired at three call sites instead of one is
a chance to forget one; at a single choke point it is a one-line fan-out.

## Component 2 — `session_id` resolution

### The field

Add to `Observation` (`loxo_llm_router/ledger.py:197`), after `usd`:

```python
session_id: str | None = None
```

and add `"session_id": self.session_id` to the dict returned by `to_entry()`. `None`
serializes as JSON `null` — a clean, forward-compatible schema addition in the same spirit
as the existing always-present `shadow` field.

### The resolver

A pure, never-raising helper — resolved once per request, testable in isolation:

```python
def resolve_session_id(
    session_header: str | None, body: dict, requested_model: str
) -> str | None:
```

Resolution order, first non-empty hit wins:

1. **`session_header`** — the value of an inbound `x-loxo-session-id` request header, when
   present and non-empty.
2. **`body.get("user")`** — the OpenAI-compatible `user` field, when a non-empty string.
3. **Body fingerprint (stateless):**
   `"sys-" + sha256(first_system_text[:512] + "\0" + first_user_text[:512] + "\0" + requested_model).hexdigest()[:16]`
   where `first_system_text` / `first_user_text` are the text of the first `system` and
   first `user` message respectively, extracted defensively (content may be a string or a
   list of parts; missing → empty string).
4. **`None`** — when nothing above resolves. The field stays `null`.

**Why the fingerprint is `system + first user message + model`, not system alone.** A
session_id must be *stable within* a conversation and *distinct across* conversations. The
system prompt and the opening user turn are both constant across a conversation's requests
(later turns append to history but never rewrite the opener), so the fingerprint is stable
turn-to-turn. The system prompt *alone* collides: a harness reuses one system prompt across
all its sessions, so `system + model` would group every OpenCode session into a single
bucket. Adding the first user message distinguishes conversations that differ in their
opener — the common case.

**Known residual (deliberate).** Two conversations with a *byte-identical* opener — same
harness, same model, same first user message (e.g. a bare slash-command or "fix the tests")
— still share a fingerprint and merge. A time-based disambiguator was considered and
rejected for the foundation: a per-request timestamp is not stable within a conversation
(the stateless router re-stamps every turn), and the only stable one — the opener's time —
requires server-side `fingerprint → first-seen-time` state, which trades away the resolver's
stateless, deterministic nature. The identical-opener case is exactly what an explicit
client-supplied id — resolution **step 1 (the `x-loxo-session-id` header)** or **step 2 (the
`user` field)** — resolves cleanly, so the capability is sited there rather than approximated
with a clock. Revisit the stateful table only if the evidence stream shows identical-opener
collisions actually mattering.

### Wiring

In the chat handler (`__init__.py:902`):

- Add `x_loxo_session_id: str | None = Header(default=None)` to the signature, mirroring
  the existing `x_quality` / `x_vision` header params.
- Call `resolve_session_id(x_loxo_session_id, body, requested_model)` after the body is
  parsed, and pass the result into the `Observation(...)` constructor (`__init__.py:999`).

**Observe-only guardrail:** `session_id` never influences routing, target selection, or the
bytes sent. It is read once at construction and only ever written to the ledger. Any
resolution failure falls through to `None`; the resolver never raises.

## Testing

New `test_tracing_foundation.py` (repo tests are flat in the root, e.g. `test_adequacy.py`):

- `test_resolve_prefers_header` — header present → returned verbatim, ahead of `user`/body.
- `test_resolve_falls_back_to_user_field` — no header, non-empty `user` → returned.
- `test_resolve_fingerprint_stable` — no header/user: same `system + first-user + model`
  yields the same id across calls; a different model (or different first user message)
  yields a different id.
- `test_resolve_none_when_no_signal` — empty/contentless body → `None`.
- `test_resolve_never_raises` — malformed message shapes (non-string content, missing keys)
  resolve to a value or `None`, never an exception.
- `test_to_entry_includes_session_id` — an `Observation` with and without a `session_id`
  serializes the key (value, then `null`).
- `test_record_writes_to_adequacy` — `record(obs)` results in the observation reaching the
  adequacy ledger (the fan-out is exercised, not just the sites).

Plus: **`test_adequacy.py` re-runs unchanged** as the refactor guard — the three finalized
paths must still land in the ledger.

## Parent-spec amendment

This design refines resolution step (c) in [otel-trace-emitter.md](otel-trace-emitter.md)
from "a stable hash of the system-prompt prefix plus requested model" to include the first
user message. The parent spec's Span-shape section should be updated to match so the two
documents do not contradict; that is a spec-only edit, made alongside this design's review.

## Sequencing

Plan A lands and can merge on its own (pure refactor + observe-only field, no new
dependency). Plan B then adds `TRACES.emit(obs)` at the `record()` seam, reads
`obs.session_id` into the `loxo.session_id` span attribute, and builds out `tracing.py`,
the child spans, the `[otel]` extra, and the `/health` block.
