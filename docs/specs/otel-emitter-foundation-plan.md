# OTel Emitter Foundation (Plan A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the emitter foundation — a `record(obs)` finalization choke point and an observe-only `session_id` on `Observation` — with no OpenTelemetry dependency.

**Architecture:** Collapse the three `Observation`-finalization sites in `forward()` into one synchronous `record(obs)` fan-out (today: the adequacy ledger only). Resolve a `session_id` once per request via a pure stateless helper (header → `user` field → `system + first-user + model` fingerprint → None) and persist it to the adequacy ledger through `Observation.to_entry()`.

**Tech Stack:** Python 3.11+ · FastAPI/Starlette · pytest (async via `asyncio.run`, no asyncio plugin) · stdlib `hashlib` only.

**Design doc:** [otel-emitter-foundation-design.md](otel-emitter-foundation-design.md) · **Parent spec:** [otel-trace-emitter.md](otel-trace-emitter.md)

## Global Constraints

- **Observe-only.** `session_id` and `record()` must never influence routing, target selection, or the bytes sent upstream. Read-once, write-to-ledger only. (Parent spec non-goal: "No new routing behavior.")
- **Fail-safe.** `resolve_session_id` never raises — any error resolves to `None`. `record()` stays fire-and-forget; a sink failure never breaks a request.
- **No new dependency.** Foundation uses stdlib only (`hashlib`). No `opentelemetry-*`, no `tracing.py` — those are Plan B.
- **Stateless resolver.** No server-side session table, no wall-clock. The resolver is a pure function of `(header, body, model)`.
- **Test conventions.** Tests are flat `test_*.py` files in the repo root. Async code is driven with `asyncio.run(...)`. `conftest.py` sets `ADEQUACY_LEDGER=""`/`SPEND_LEDGER=""` and isolates config suite-wide; never rely on the module `ADEQUACY` singleton writing to disk.
- **Branch.** All work on `feat/otel-trace-emitter`.

---

### Task 1: `record(obs)` finalization choke point

Collapse the three `_spawn(ADEQUACY.write(obs))` sites in `forward()` into one `record(obs)`. Pure refactor: zero behavior change. `test_adequacy.py` is the guard that all three paths still reach the ledger.

**Files:**
- Modify: `loxo_llm_router/__init__.py` — add `record()` after the `ADEQUACY` singleton (~`:365`); replace the three call sites (`:796`, `:828`, `:888`).
- Create: `test_tracing_foundation.py` (repo root).

**Interfaces:**
- Consumes: module globals `ADEQUACY` (`AdequacyLedger`), `_spawn(coro)`, and `_BACKGROUND_TASKS` (a `set`), all in `loxo_llm_router/__init__.py`.
- Produces: `record(obs: Observation) -> None` — synchronous; schedules a fire-and-forget `ADEQUACY.write(obs)`. Later tasks/plans add sinks here.

- [ ] **Step 1: Write the failing test**

Create `test_tracing_foundation.py`:

```python
"""Tests for the OTel emitter foundation (Plan A): record() choke point + session_id."""

import asyncio

import loxo_llm_router as R
from loxo_llm_router import ledger


def _obs(**kw):
    base = dict(cls="main", classifier_version=1, requested_model="loxo/auto",
                route="cloud", served_model="z-ai/glm-5.2", reason="virtual-quality-best",
                stream=False)
    base.update(kw)
    return ledger.Observation(**base)


def test_record_fans_out_to_adequacy(monkeypatch):
    seen = []

    async def fake_write(obs):
        seen.append(obs)

    monkeypatch.setattr(R.ADEQUACY, "write", fake_write)
    o = _obs()

    async def drive():
        R.record(o)
        # record() scheduled a fire-and-forget task; drain it deterministically.
        await asyncio.gather(*list(R._BACKGROUND_TASKS))

    asyncio.run(drive())
    assert seen == [o]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_tracing_foundation.py::test_record_fans_out_to_adequacy -v`
Expected: FAIL with `AttributeError: module 'loxo_llm_router' has no attribute 'record'`.

- [ ] **Step 3: Add the `record()` function**

In `loxo_llm_router/__init__.py`, immediately after the `ADEQUACY = AdequacyLedger(...)` line (~`:365`):

```python
def record(obs: Observation) -> None:
    """Single finalization choke point: fan a completed obs out to every sink.

    Synchronous — only schedules fire-and-forget work, so it is safe from the
    streaming error path, the streamer() generator, and the non-streaming path.
    Plan B (OTel) adds the second sink at THIS one site, not three.
    """
    _spawn(ADEQUACY.write(obs))
    # Plan B: _spawn(TRACES.emit(obs))  — the emitter hooks here.
```

(`Observation` is already imported into `__init__.py` from `.ledger`; if the type name is not in scope, use a string annotation `obs: "Observation"`.)

- [ ] **Step 4: Replace the three call sites**

In `forward()`, replace the line `_spawn(ADEQUACY.write(obs))` with `record(obs)` at all three finalization sites:
- `:796` — streaming non-200 error path (inside `if obs is not None:`, before `return JSONResponse(...)`).
- `:828` — streaming-success tail (inside `streamer()`, end of the `if obs is not None:` block).
- `:888` — non-streaming path (inside `if obs is not None:`, before building `content`).

Each becomes exactly:

```python
            record(obs)
```

(match the existing indentation at each site).

- [ ] **Step 5: Run the new test and the refactor guard**

Run: `pytest test_tracing_foundation.py::test_record_fans_out_to_adequacy test_adequacy.py -v`
Expected: PASS — the new fan-out test passes, and every existing adequacy test stays green (the three paths still write).

- [ ] **Step 6: Commit**

```bash
git add loxo_llm_router/__init__.py test_tracing_foundation.py
git commit -m "refactor: collapse forward() finalization into record(obs) choke point"
```

---

### Task 2: `session_id` field on `Observation` + ledger persistence

Add the field and serialize it in `to_entry()`. Update the exact-schema guard test in `test_adequacy.py` — the schema legitimately grows by one key.

**Files:**
- Modify: `loxo_llm_router/ledger.py` — `Observation` dataclass (`:197`) and `to_entry()` (`:218`).
- Modify: `test_adequacy.py` — `test_entry_has_exact_spec_schema` expected key set.
- Modify: `test_tracing_foundation.py` — add `test_to_entry_includes_session_id`.

**Interfaces:**
- Consumes: `ledger.Observation` from Task 0 baseline.
- Produces: `Observation.session_id: str | None` (default `None`), present in `to_entry()` output under key `"session_id"`.

- [ ] **Step 1: Write the failing test**

Append to `test_tracing_foundation.py`:

```python
def test_to_entry_includes_session_id():
    assert _obs(session_id="sess-1").to_entry()["session_id"] == "sess-1"
    assert _obs().to_entry()["session_id"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_tracing_foundation.py::test_to_entry_includes_session_id -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'session_id'` (field not yet defined).

- [ ] **Step 3: Add the field**

In `loxo_llm_router/ledger.py`, in the `Observation` dataclass, add after the `usd: float = 0.0` line (`:216`):

```python
    session_id: str | None = None
```

- [ ] **Step 4: Serialize it**

In `Observation.to_entry()` (`:218`), add a `"session_id"` entry to the returned dict — place it just before `"shadow": False`:

```python
            "session_id": self.session_id,
            "shadow": False,
```

- [ ] **Step 5: Update the exact-schema guard**

In `test_adequacy.py`, `test_entry_has_exact_spec_schema`, add `"session_id"` to the expected key set:

```python
    assert set(e) == {"ts", "class", "classifier_version", "requested_model", "route",
                      "served_model", "reason", "stream", "status", "latency_ms",
                      "ttfb_ms", "fallback_fired", "finish_reason", "had_tool_calls",
                      "tool_calls_valid_json", "tokens", "usd", "session_id", "shadow"}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest test_tracing_foundation.py::test_to_entry_includes_session_id test_adequacy.py -v`
Expected: PASS — new test passes and the updated schema guard passes.

- [ ] **Step 7: Commit**

```bash
git add loxo_llm_router/ledger.py test_adequacy.py test_tracing_foundation.py
git commit -m "feat: add observe-only session_id to Observation + adequacy ledger"
```

---

### Task 3: `resolve_session_id` stateless resolver

A pure helper resolving the session id from header → `user` → body fingerprint → None. Never raises.

**Files:**
- Modify: `loxo_llm_router/__init__.py` — add `_first_message_text()` and `resolve_session_id()` (place them near the other request-body helpers; above the `chat_completions` handler is fine).
- Modify: `test_tracing_foundation.py` — add resolver tests.

**Interfaces:**
- Produces:
  - `_first_message_text(messages: list, role: str) -> str` — text of the first message with `role`, joining `{"type":"text"}` parts; `""` if absent/odd.
  - `resolve_session_id(session_header: str | None, body: dict, requested_model: str) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `test_tracing_foundation.py`:

```python
def test_resolve_prefers_header():
    body = {"user": "u", "messages": [{"role": "user", "content": "hi"}]}
    assert R.resolve_session_id("hdr-1", body, "loxo/auto") == "hdr-1"


def test_resolve_falls_back_to_user_field():
    body = {"user": "user-42", "messages": [{"role": "user", "content": "hi"}]}
    assert R.resolve_session_id(None, body, "loxo/auto") == "user-42"
    assert R.resolve_session_id("", body, "loxo/auto") == "user-42"  # empty header ignored


def test_resolve_fingerprint_stable_and_sensitive():
    body = {"messages": [{"role": "system", "content": "SYS"},
                         {"role": "user", "content": "OPEN"}]}
    a = R.resolve_session_id(None, body, "loxo/auto")
    assert a == R.resolve_session_id(None, body, "loxo/auto")  # stable
    assert a.startswith("sys-")
    assert R.resolve_session_id(None, body, "loxo/other") != a  # model changes id
    body2 = {"messages": [{"role": "system", "content": "SYS"},
                          {"role": "user", "content": "DIFFERENT"}]}
    assert R.resolve_session_id(None, body2, "loxo/auto") != a  # first user msg changes id


def test_resolve_none_when_no_content():
    assert R.resolve_session_id(None, {}, "loxo/auto") is None
    assert R.resolve_session_id(None, {"messages": []}, "loxo/auto") is None


def test_resolve_never_raises_on_odd_shapes():
    body = {"user": 123,  # non-str user is ignored, not an error
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                         {"role": "system"}]}  # system msg with no content
    out = R.resolve_session_id(None, body, "loxo/auto")
    assert out.startswith("sys-")  # fingerprint over the user text "hi"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_tracing_foundation.py -k resolve -v`
Expected: FAIL with `AttributeError: module 'loxo_llm_router' has no attribute 'resolve_session_id'`.

- [ ] **Step 3: Implement the helpers**

In `loxo_llm_router/__init__.py`, ensure `import hashlib` is present at the top, then add above the `chat_completions` handler:

```python
def _first_message_text(messages: list, role: str) -> str:
    """Text of the first message with `role`. Content may be a string or a list
    of parts; joins the {"type":"text"} parts. Returns "" if absent or odd-shaped."""
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != role:
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return ""
    return ""


def resolve_session_id(
    session_header: str | None, body: dict, requested_model: str
) -> str | None:
    """Resolve an observe-only session id. Never raises (odd shapes -> None).

    Order, first hit wins:
      1. x-loxo-session-id header (explicit, client-supplied)
      2. OpenAI-compatible `user` field (explicit, client-supplied)
      3. stateless fingerprint: system prefix + first user message + model
      4. None (no content to fingerprint)
    """
    try:
        if isinstance(session_header, str) and session_header.strip():
            return session_header
        user = body.get("user")
        if isinstance(user, str) and user.strip():
            return user
        messages = body.get("messages") or []
        sys_text = _first_message_text(messages, "system")[:512]
        usr_text = _first_message_text(messages, "user")[:512]
        if not (sys_text or usr_text):
            return None
        raw = f"{sys_text}\x00{usr_text}\x00{requested_model}".encode("utf-8")
        return "sys-" + hashlib.sha256(raw).hexdigest()[:16]
    except Exception:  # noqa: BLE001 - observe-only: resolution must never break a request
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest test_tracing_foundation.py -k resolve -v`
Expected: PASS (all five resolver tests).

- [ ] **Step 5: Commit**

```bash
git add loxo_llm_router/__init__.py test_tracing_foundation.py
git commit -m "feat: stateless resolve_session_id (header -> user -> fingerprint -> None)"
```

---

### Task 4: Wire the resolver into the chat handler

Bind the `x-loxo-session-id` header, resolve once, and pass the result into the `Observation` constructor. Verified end-to-end through the real handler with `forward` stubbed.

**Files:**
- Modify: `loxo_llm_router/__init__.py` — `chat_completions` signature (`:902`), resolve after body parse (~`:915`), pass `session_id=` into `Observation(...)` (`:999`).
- Modify: `test_tracing_foundation.py` — add handler-wiring tests.

**Interfaces:**
- Consumes: `resolve_session_id` (Task 3), `Observation.session_id` (Task 2), module globals `app`, `forward`.
- Produces: the handler constructs its `Observation` with the resolved `session_id`, so it reaches `record()` → the adequacy ledger.

- [ ] **Step 1: Write the failing tests**

Append to `test_tracing_foundation.py`:

```python
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse


def _wire_client(monkeypatch):
    """A TestClient whose forward() is stubbed to capture the Observation."""
    captured = {}

    async def fake_forward(*args, **kwargs):
        captured["obs"] = kwargs.get("obs")
        return JSONResponse({"ok": True})

    monkeypatch.setattr(R, "forward", fake_forward)
    return TestClient(R.app), captured


def test_handler_wires_session_id_from_header(monkeypatch):
    client, captured = _wire_client(monkeypatch)
    resp = client.post(
        "/v1/chat/completions",
        headers={"x-loxo-session-id": "sess-abc"},
        json={"model": "loxo/auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert captured["obs"].session_id == "sess-abc"


def test_handler_wires_session_id_from_user_field(monkeypatch):
    client, captured = _wire_client(monkeypatch)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "loxo/auto", "user": "user-77",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert captured["obs"].session_id == "user-77"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_tracing_foundation.py -k handler -v`
Expected: FAIL — `captured["obs"].session_id` is `None` (handler does not yet resolve/pass it), so the assertion fails.

- [ ] **Step 3: Add the header parameter**

In the `chat_completions` signature (`:902`), add the header param alongside `x_quality` / `x_vision`:

```python
async def chat_completions(
    request: Request,
    x_quality: str | None = Header(default=None),
    x_vision: str | None = Header(default=None),
    x_loxo_session_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
```

- [ ] **Step 4: Resolve after the body is parsed**

Just after `requested_model = body.get("model", "")` (~`:915`), add:

```python
    session_id = resolve_session_id(x_loxo_session_id, body, requested_model)
```

- [ ] **Step 5: Pass it into the Observation**

In the `Observation(...)` constructor (`:999`), add the `session_id` kwarg:

```python
    obs = Observation(
        cls=klass.cls, classifier_version=klass.version,
        requested_model=requested_model,
        route="cloud" if is_cloud else "local",
        served_model=model_to_send, reason=reason, stream=stream,
        session_id=session_id,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest test_tracing_foundation.py -k handler -v`
Expected: PASS — both wiring tests see the resolved id on the captured `Observation`.

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: PASS — entire suite green, confirming no regression (routing, metadata, adequacy, cache).

- [ ] **Step 8: Commit**

```bash
git add loxo_llm_router/__init__.py test_tracing_foundation.py
git commit -m "feat: resolve x-loxo-session-id in chat handler, thread into Observation"
```

---

## Self-Review

**Spec coverage (against the design doc):**
- Component 1 `record(obs)` choke point → Task 1. ✓
- Three sites `:796/:828/:888` collapsed → Task 1 Step 4. ✓
- `Observation.session_id` field + `to_entry()` persistence → Task 2. ✓
- Resolver order (header → user → `system+first-user+model` fingerprint → None) → Task 3. ✓
- Stateless / never-raises guardrails → Task 3 tests + implementation. ✓
- Handler wiring incl. `x-loxo-session-id` header binding → Task 4. ✓
- Adequacy suite as refactor guard → Task 1 Step 5. ✓
- Exact-schema guard updated for the new key → Task 2 Step 5. ✓ (a spec-coverage trap: the design's "adequacy tests stay green" is true for behavior, but the schema-pinning test must be edited because the schema itself grows.)

**Scope boundary (out of scope, confirmed absent):** no `tracing.py`, no span, no `loxo.session_id` span attribute, no `[otel]` extra, no `/health` block. `record()` carries only a comment seam for `TRACES.emit`. ✓

**Type consistency:** `resolve_session_id(session_header, body, requested_model) -> str | None` and `Observation.session_id: str | None` are used identically in Tasks 2–4. `record(obs) -> None` consumes `ADEQUACY`/`_spawn`/`_BACKGROUND_TASKS` as they exist in `__init__.py`. ✓

**Placeholder scan:** none — every step carries concrete code. ✓
