# Virtual Model Abstraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the OpenCode config's GLM-5.2 hacks (image-capability lie, hardcoded per-provider cost, leaked upstream id) with a single honest `airwolf/auto` virtual model owned by the router, plus a live rate card from OpenRouter pricing.

**Architecture:** Add an inline Python `VirtualModel` registry to the single-file `llm_router.py`. `pick_target` gains a top-priority branch that resolves a virtual id to a real upstream model via the existing local/cloud heuristics. A lazy, non-blocking rate-card fetch surfaces live pricing on `/health` and `/v1/spend`; `usage.cost` actuals are untouched. The OpenCode config collapses to one honest entry.

**Tech Stack:** Python 3.14, FastAPI, httpx, pytest 9.x (dev). Router runs under the venv `/Users/brendanl/.venvs/mlx/bin/python3`.

## Global Constraints

- **Single file:** all router logic stays in `llm_router.py`. No new runtime modules.
- **Tests:** `test_routing.py` at repo root; run `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q` from repo root. No network in unit tests.
- **Never block or fail a request because pricing is unavailable.** Rate-card fetch is best-effort; on any failure the cards stay empty/stale and endpoints still respond.
- **Backward compatible:** raw ids (`z-ai/glm-5.2`, `mlx-community/...`), `x-quality`, size gating, and non-virtual transport fallback behave exactly as today. New behavior triggers only on registered virtual ids.
- **Deployment ids are env-overridable:** `AUTO_MODEL_ID` (default `airwolf/auto`), `AUTO_CLOUD_MODEL` (default `z-ai/glm-5.2`), `AUTO_LOCAL_MODEL` (default unset → first `LOCAL_MODELS` entry), `RATE_CARD_TTL` (default `86400`), `RATE_CARD_URL` (default `https://openrouter.ai/api/v1/models`).
- **Spec:** `docs/superpowers/specs/2026-06-22-virtual-model-abstraction-design.md`.

---

### Task 1: VirtualModel registry + resolver

**Files:**
- Modify: `llm_router.py` (add `dataclass` import near top with the other stdlib imports; add registry block after the `LOCAL_MODELS` env block, ~line 87)
- Test: `test_routing.py`

**Interfaces:**
- Produces: `VirtualModel` frozen dataclass with fields `id: str`, `cloud_target: str`, `local_target: str | None = None`, `vision: bool = True`, `advertised_context: int = 1_048_576`; module global `VIRTUAL_MODELS: dict[str, VirtualModel]`; `resolve_virtual(model_id: str) -> VirtualModel | None`.

- [ ] **Step 1: Write the failing tests** — append to `test_routing.py`:

```python
def test_resolve_virtual_known():
    vm = R.resolve_virtual("airwolf/auto")
    assert vm is not None
    assert vm.id == "airwolf/auto"
    assert vm.cloud_target == "z-ai/glm-5.2"
    assert vm.vision is True


def test_resolve_virtual_unknown_returns_none():
    assert R.resolve_virtual("z-ai/glm-5.2") is None
    assert R.resolve_virtual("") is None
```

Note: these two tests assert the *default* registry built from env. They do not use the `routing_env` fixture's patched registry — they run against the real module-level `VIRTUAL_MODELS`. Keep them independent of `AUTO_CLOUD_MODEL` by running without that env var set (the default is `z-ai/glm-5.2`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k resolve_virtual -q`
Expected: FAIL with `AttributeError: module 'llm_router' has no attribute 'resolve_virtual'`

- [ ] **Step 3: Add the import.** At the top of `llm_router.py`, in the stdlib import group (near `import asyncio`), add:

```python
from dataclasses import dataclass
```

- [ ] **Step 4: Add the registry block** in `llm_router.py` immediately after the `LOCAL_MODELS = {...}` block (~line 87):

```python
@dataclass(frozen=True)
class VirtualModel:
    """A client-facing model id the router resolves to real upstream models.

    The abstraction lives here, in code: one entry bundles the cloud/local
    targets, the vision intent, and the advertised context window. Deployment
    ids are env-overridable; the structure and intent are legible in one place.
    """
    id: str
    cloud_target: str
    local_target: str | None = None
    vision: bool = True
    advertised_context: int = 1_048_576


VIRTUAL_MODELS: dict[str, VirtualModel] = {
    vm.id: vm for vm in (
        VirtualModel(
            id=os.environ.get("AUTO_MODEL_ID", "airwolf/auto"),
            cloud_target=os.environ.get("AUTO_CLOUD_MODEL", "z-ai/glm-5.2"),
            local_target=os.environ.get("AUTO_LOCAL_MODEL") or None,
        ),
    )
}


def resolve_virtual(model_id: str) -> VirtualModel | None:
    """Return the VirtualModel for a client-facing id, or None for raw ids."""
    return VIRTUAL_MODELS.get(model_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k resolve_virtual -q`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: add VirtualModel registry + resolve_virtual"
```

---

### Task 2: Routing branch-0 (virtual resolution) + local-target fallback

**Files:**
- Modify: `llm_router.py` — change the `LOCAL_MODELS` construction (~line 85) to also keep insertion order; add `local_target_for`; add branch 0 at the top of `pick_target` (~line 456)
- Test: `test_routing.py`

**Interfaces:**
- Consumes: `VirtualModel`, `resolve_virtual` (Task 1); existing `estimate_prompt_tokens`, `LOCAL_CONTEXT_LIMIT`, `LOCAL_BASE_URL`, `CLOUD_BASE_URL`.
- Produces: module global `LOCAL_MODELS_ORDER: list[str]`; `local_target_for(vm: VirtualModel, fallback_model: str) -> str`; `pick_target` returns reasons `"virtual-quality-best"`, `"virtual-prompt-too-long"`, `"virtual-local"` for virtual ids, with `model_to_send` set to the resolved real upstream id.

- [ ] **Step 1: Write the failing tests** — append to `test_routing.py`. First, extend the autouse fixture to install a deterministic virtual registry and ordered local list (add these two lines inside `routing_env`, before `yield`):

```python
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Qwen3.6-35B-A3B-4bit"])
    monkeypatch.setattr(R, "VIRTUAL_MODELS", {
        "airwolf/auto": R.VirtualModel(id="airwolf/auto", cloud_target="z-ai/glm-5.2"),
    })
```

Then add the tests:

```python
def test_virtual_small_prompt_routes_local_with_resolved_id():
    base, model, reason = R.pick_target(_body(model="airwolf/auto", text="hi"), None)
    assert base == R.LOCAL_BASE_URL
    assert model == "mlx-community/Qwen3.6-35B-A3B-4bit"  # virtual id NOT forwarded
    assert reason == "virtual-local"


def test_virtual_quality_best_routes_cloud_target():
    base, model, reason = R.pick_target(_body(model="airwolf/auto"), "best")
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-5.2"
    assert reason == "virtual-quality-best"


def test_virtual_large_prompt_routes_cloud_target(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 10)
    base, model, reason = R.pick_target(_body(model="airwolf/auto", text="x" * 1000), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-5.2"
    assert reason == "virtual-prompt-too-long"


def test_virtual_slash_is_not_treated_as_provider_prefixed():
    # airwolf/auto contains "/" but must NOT fall to the provider-prefixed rule.
    _, _, reason = R.pick_target(_body(model="airwolf/auto", text="hi"), None)
    assert reason.startswith("virtual")


def test_local_target_for_explicit_override():
    vm = R.VirtualModel(id="x", cloud_target="c", local_target="custom-local")
    assert R.local_target_for(vm, "fallback") == "custom-local"


def test_local_target_for_empty_models_uses_fallback(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", [])
    vm = R.VirtualModel(id="x", cloud_target="c")
    assert R.local_target_for(vm, "airwolf/auto") == "airwolf/auto"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k "virtual or local_target" -q`
Expected: FAIL (`AttributeError` on `local_target_for` / `LOCAL_MODELS_ORDER`, or wrong reason)

- [ ] **Step 3: Make `LOCAL_MODELS` order-preserving.** Replace the existing block (~line 85):

```python
LOCAL_MODELS = {
    m.strip() for m in os.environ.get("LOCAL_MODELS", "").split(",") if m.strip()
}
```

with:

```python
LOCAL_MODELS_ORDER = [
    m.strip() for m in os.environ.get("LOCAL_MODELS", "").split(",") if m.strip()
]
LOCAL_MODELS = set(LOCAL_MODELS_ORDER)
```

- [ ] **Step 4: Add `local_target_for`** just above `pick_target` (~line 455):

```python
def local_target_for(vm: VirtualModel, fallback_model: str) -> str:
    """Resolve the real local model id to send for a virtual request.

    Order: explicit vm.local_target -> first configured LOCAL_MODELS entry ->
    the caller's original model id (degrade, don't crash if no local models).
    """
    if vm.local_target:
        return vm.local_target
    if LOCAL_MODELS_ORDER:
        return LOCAL_MODELS_ORDER[0]
    return fallback_model
```

- [ ] **Step 5: Add branch 0** at the very top of `pick_target`, immediately after `model = body.get("model", "")`:

```python
    vm = resolve_virtual(model)
    if vm is not None:
        if (quality_header or "").lower() == "best":
            return CLOUD_BASE_URL, vm.cloud_target, "virtual-quality-best"
        if estimate_prompt_tokens(body) > LOCAL_CONTEXT_LIMIT:
            return CLOUD_BASE_URL, vm.cloud_target, "virtual-prompt-too-long"
        return LOCAL_BASE_URL, local_target_for(vm, model), "virtual-local"
```

- [ ] **Step 6: Run the full test file to verify pass + no regressions**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: PASS (all prior 14 + new tests green)

- [ ] **Step 7: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: route virtual ids via branch-0 with local-target fallback"
```

---

### Task 3: Thread vision intent + virtual resolution into the request handler

**Files:**
- Modify: `llm_router.py` — add `vision_enabled` param to `apply_vision_policy` (~line 397); add `fallback_cloud_model` param to `forward` (~line 502) and use it at both fallback sites (~lines 563, 590); capture original model + vm in `chat_completions` (~line 625), pass `vm.vision`, and set the fallback model to `vm.cloud_target` for virtual requests (~line 658)
- Test: `test_routing.py`

**Interfaces:**
- Consumes: `resolve_virtual` (Task 1), `apply_vision_policy` + `forward` (existing).
- Produces: `apply_vision_policy(..., vision_enabled: bool = True)` — when `False`, returns inputs unchanged (images pass through, no shim/reroute). `forward(..., fallback_cloud_model: str | None = None)` — the cloud model attributed when the local→cloud transport fallback fires (defaults to `CLOUD_DEFAULT_MODEL`).

- [ ] **Step 1: Write the failing test** — append to `test_routing.py` (uses `asyncio.run`, no pytest-asyncio needed because the disabled path never awaits the network):

```python
import asyncio


def test_vision_policy_disabled_short_circuits():
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]}
    out = asyncio.run(R.apply_vision_policy(
        body, R.LOCAL_BASE_URL, "qwen-local", "virtual-local", None, vision_enabled=False
    ))
    assert out == (R.LOCAL_BASE_URL, "qwen-local", body, "virtual-local")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k vision_policy_disabled -q`
Expected: FAIL with `TypeError: apply_vision_policy() got an unexpected keyword argument 'vision_enabled'`

- [ ] **Step 3: Add the param + short-circuit.** Change the `apply_vision_policy` signature (~line 397) from:

```python
async def apply_vision_policy(
    body: dict[str, Any],
    base_url: str,
    model_to_send: str,
    reason: str,
    x_vision: str | None,
) -> tuple[str, str, dict[str, Any], str]:
```

to add the parameter:

```python
async def apply_vision_policy(
    body: dict[str, Any],
    base_url: str,
    model_to_send: str,
    reason: str,
    x_vision: str | None,
    vision_enabled: bool = True,
) -> tuple[str, str, dict[str, Any], str]:
```

and insert as the first line of the function body (before `if is_vision_capable(...)`):

```python
    if not vision_enabled:
        return base_url, model_to_send, body, reason
```

- [ ] **Step 4: Wire `chat_completions`.** In `chat_completions` (~line 621), capture the original model and its virtual record before routing, and pass the vision intent. Change:

```python
    body_bytes = await request.body()
    body = json.loads(body_bytes)
    stream = bool(body.get("stream", False))

    base_url, model_to_send, reason = pick_target(body, x_quality)
    body["model"] = model_to_send
```

to:

```python
    body_bytes = await request.body()
    body = json.loads(body_bytes)
    stream = bool(body.get("stream", False))

    requested_vm = resolve_virtual(body.get("model", ""))
    base_url, model_to_send, reason = pick_target(body, x_quality)
    body["model"] = model_to_send
```

and change the `apply_vision_policy` call (~line 631) from:

```python
    base_url, model_to_send, body, reason = await apply_vision_policy(
        body, base_url, model_to_send, reason, x_vision
    )
```

to:

```python
    base_url, model_to_send, body, reason = await apply_vision_policy(
        body, base_url, model_to_send, reason, x_vision,
        vision_enabled=(requested_vm.vision if requested_vm else True),
    )
```

- [ ] **Step 5: Point the transport fallback at `vm.cloud_target` for virtual requests.** This is a network-path change (verified manually in Task 6, Step 5), so there's no unit test; make the edits exactly.

First, add a parameter to `forward` (~line 502). Change the signature's tail:

```python
    cloud_model: str | None = None,
    cloud_provider: str | None = None,
    reason: str = "",
):
```

to:

```python
    cloud_model: str | None = None,
    cloud_provider: str | None = None,
    reason: str = "",
    fallback_cloud_model: str | None = None,
):
```

and at the **streaming** fallback site (~line 560), change:

```python
                            log(f"[router] stream: {url} unreachable pre-first-byte, "
                                f"falling back to cloud/{CLOUD_DEFAULT_MODEL}")
                            url, body = fallback_url, fallback_body  # type: ignore[assignment]
                            served_cloud_model = CLOUD_DEFAULT_MODEL
```

to:

```python
                            fb_model = fallback_cloud_model or CLOUD_DEFAULT_MODEL
                            log(f"[router] stream: {url} unreachable pre-first-byte, "
                                f"falling back to cloud/{fb_model}")
                            url, body = fallback_url, fallback_body  # type: ignore[assignment]
                            served_cloud_model = fb_model
```

and at the **non-streaming** fallback site (~line 585), change:

```python
            log(f"[router] {primary_url} unreachable, falling back to cloud/{CLOUD_DEFAULT_MODEL}")
            resp = await client.post(
                f"{fallback_url}{path}", content=fallback_body,
                headers=_headers_for(fallback_url, client_headers),
            )
            served_cloud_model = CLOUD_DEFAULT_MODEL
```

to:

```python
            fb_model = fallback_cloud_model or CLOUD_DEFAULT_MODEL
            log(f"[router] {primary_url} unreachable, falling back to cloud/{fb_model}")
            resp = await client.post(
                f"{fallback_url}{path}", content=fallback_body,
                headers=_headers_for(fallback_url, client_headers),
            )
            served_cloud_model = fb_model
```

- [ ] **Step 6: Build the fallback with the virtual cloud target.** In `chat_completions`, change the fallback-construction block (~line 656):

```python
    fallback_url: str | None = None
    fallback_body: bytes | None = None
    if base_url == LOCAL_BASE_URL:
        fb = dict(body)
        fb["model"] = CLOUD_DEFAULT_MODEL
        if stream:
            fb["stream_options"] = {**fb.get("stream_options", {}), "include_usage": True}
        fallback_url = CLOUD_BASE_URL
        fallback_body = json.dumps(fb).encode()
```

to:

```python
    fallback_url: str | None = None
    fallback_body: bytes | None = None
    fallback_cloud_model = requested_vm.cloud_target if requested_vm else CLOUD_DEFAULT_MODEL
    if base_url == LOCAL_BASE_URL:
        fb = dict(body)
        fb["model"] = fallback_cloud_model
        if stream:
            fb["stream_options"] = {**fb.get("stream_options", {}), "include_usage": True}
        fallback_url = CLOUD_BASE_URL
        fallback_body = json.dumps(fb).encode()
```

and pass it through in the `return await forward(...)` call (~line 671) by adding the new kwarg:

```python
    return await forward(
        base_url, "/chat/completions", primary_body, dict(request.headers), stream,
        fallback_url=fallback_url, fallback_body=fallback_body,
        cloud_model=served_cloud_model, cloud_provider=served_cloud_provider, reason=reason,
        fallback_cloud_model=fallback_cloud_model,
    )
```

- [ ] **Step 7: Run tests + import check to verify no regressions**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q && /Users/brendanl/.venvs/mlx/bin/python3 -c "import llm_router; print('import ok')"`
Expected: PASS (all green); `import ok`

- [ ] **Step 8: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: gate vision on virtual intent; fail virtual requests over to vm.cloud_target"
```

---

### Task 4: Live rate card (parser + lazy non-blocking fetch + endpoints)

**Files:**
- Modify: `llm_router.py` — add rate-card globals + `_parse_rate_card` + `get_rate_cards` + `_rate_cards_snapshot_and_maybe_refresh` (after the spend accumulator block, ~line 193); add `rate_cards` to `/v1/spend` (~line 707) and `/health` (~line 735)
- Test: `test_routing.py`

**Interfaces:**
- Consumes: `VIRTUAL_MODELS` (Task 1), existing `log`, `httpx`, `datetime`.
- Produces: `_parse_rate_card(models_payload: dict, target_id: str) -> dict | None`; `async get_rate_cards() -> dict[str, dict]`; `_rate_cards_snapshot_and_maybe_refresh() -> dict[str, dict]`; module globals `RATE_CARD_TTL`, `RATE_CARD_URL`.

- [ ] **Step 1: Write the failing tests** (parser only — pure, no network) — append to `test_routing.py`:

```python
_SAMPLE_MODELS_PAYLOAD = {"data": [
    {"id": "z-ai/glm-5.2", "context_length": 1048576,
     "architecture": {"input_modalities": ["text"]},
     "pricing": {"prompt": "0.000001", "completion": "0.000004",
                 "input_cache_read": "0.00000018"}},
    {"id": "other/model", "pricing": {"prompt": "0.000002"}},
]}


def test_parse_rate_card_extracts_per_mtok():
    card = R._parse_rate_card(_SAMPLE_MODELS_PAYLOAD, "z-ai/glm-5.2")
    assert card is not None
    assert card["input_per_mtok"] == 1.0
    assert card["output_per_mtok"] == 4.0
    assert card["cache_read_per_mtok"] == 0.18
    assert card["context_length"] == 1048576
    assert card["input_modalities"] == ["text"]


def test_parse_rate_card_missing_fields_are_none():
    card = R._parse_rate_card(_SAMPLE_MODELS_PAYLOAD, "other/model")
    assert card is not None
    assert card["input_per_mtok"] == 2.0
    assert card["output_per_mtok"] is None
    assert card["cache_read_per_mtok"] is None


def test_parse_rate_card_unknown_id_returns_none():
    assert R._parse_rate_card(_SAMPLE_MODELS_PAYLOAD, "nope/nope") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k parse_rate_card -q`
Expected: FAIL with `AttributeError: ... has no attribute '_parse_rate_card'`

- [ ] **Step 3: Add the rate-card block** in `llm_router.py` after the spend accumulator section (after `record_cost`, ~line 193):

```python
# --- Live rate card -------------------------------------------------------------
# Fetch each virtual model's cloud_target price from OpenRouter and expose it
# read-only. Purely informational: never blocks or fails a request. Lazy with a
# TTL; the snapshot helper returns the current cache and schedules a background
# refresh when stale, so endpoints never await the network.
RATE_CARD_TTL = float(os.environ.get("RATE_CARD_TTL", "86400"))
RATE_CARD_URL = os.environ.get("RATE_CARD_URL", "https://openrouter.ai/api/v1/models")

_rate_cards: dict[str, dict[str, Any]] = {}
_rate_cards_fetched_at: float | None = None
_rate_card_lock = asyncio.Lock()


def _parse_rate_card(models_payload: dict[str, Any], target_id: str) -> dict[str, Any] | None:
    """Extract one model's rate card from an OpenRouter /models payload.

    Pricing fields are USD-per-token strings; we convert to USD-per-Mtok.
    Returns None if the id isn't present. Missing price fields become None.
    """
    for m in models_payload.get("data", []):
        if m.get("id") != target_id:
            continue
        pricing = m.get("pricing", {}) or {}
        arch = m.get("architecture", {}) or {}

        def per_mtok(key: str) -> float | None:
            v = pricing.get(key)
            return round(float(v) * 1_000_000, 6) if v is not None else None

        return {
            "model": target_id,
            "input_per_mtok": per_mtok("prompt"),
            "output_per_mtok": per_mtok("completion"),
            "cache_read_per_mtok": per_mtok("input_cache_read"),
            "context_length": m.get("context_length"),
            "input_modalities": arch.get("input_modalities"),
        }
    return None


async def get_rate_cards() -> dict[str, dict[str, Any]]:
    """Fetch + cache rate cards for every distinct cloud_target. Best-effort:
    on any failure, leaves the existing cache untouched and returns it."""
    global _rate_cards_fetched_at
    import time
    async with _rate_card_lock:
        targets = {vm.cloud_target for vm in VIRTUAL_MODELS.values()}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(RATE_CARD_URL)
            payload = r.json() if r.status_code == 200 else {}
        except Exception as e:  # noqa: BLE001 - pricing is never request-critical
            log(f"[router] rate-card fetch failed ({e}); using stale/empty cards")
            return dict(_rate_cards)

        cards: dict[str, dict[str, Any]] = {}
        for t in targets:
            card = _parse_rate_card(payload, t)
            if not card:
                continue
            card["fetched_at"] = datetime.now(timezone.utc).isoformat()
            cards[t] = card
            # Self-check: a virtual model declaring vision whose cloud_target is
            # text-only needs the shim. Assert the former config lie in code.
            for vm in VIRTUAL_MODELS.values():
                if vm.cloud_target == t and vm.vision and card.get("input_modalities") == ["text"]:
                    log(f"[router] vision shim required for cloud_target {t} (text-only)")

        if cards:
            _rate_cards.clear()
            _rate_cards.update(cards)
            _rate_cards_fetched_at = time.monotonic()
        return dict(_rate_cards)


def _rate_cards_snapshot_and_maybe_refresh() -> dict[str, dict[str, Any]]:
    """Return the current cache immediately; schedule a refresh if stale.
    Non-blocking — endpoints never await the pricing fetch."""
    import time
    now = time.monotonic()
    stale = _rate_cards_fetched_at is None or (now - _rate_cards_fetched_at) >= RATE_CARD_TTL
    if stale:
        asyncio.ensure_future(get_rate_cards())
    return dict(_rate_cards)
```

- [ ] **Step 4: Run parser tests to verify pass**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k parse_rate_card -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Surface `rate_cards` on `/v1/spend`.** In the `spend` endpoint, inside the `async with _spend_lock:` return dict, add a key (it's fine to call the non-blocking snapshot helper outside the lock; restructure so the snapshot is computed first):

Change the body of `spend` from returning the dict directly to:

```python
    cards = _rate_cards_snapshot_and_maybe_refresh()
    async with _spend_lock:
        return {
            "total_usd": round(_spend_total_usd, 8),
            "requests": _spend_requests,
            "since": _spend_since,
            "ledger": str(SPEND_LEDGER) if SPEND_LEDGER else None,
            "rate_cards": cards,
            "by_provider": {
                provider: {
                    "total_usd": round(pv["total_usd"], 8),
                    "requests": pv["requests"],
                    "by_model": {
                        m: {"usd": round(mv["usd"], 8), "requests": mv["requests"]}
                        for m, mv in sorted(pv["by_model"].items())
                    },
                }
                for provider, pv in sorted(_spend_by_provider.items())
            },
        }
```

- [ ] **Step 6: Surface `rate_cards` on `/health`.** In the `health` endpoint, add the snapshot and include it in the returned dict:

Change the start of `health` to compute the snapshot, and add `"rate_cards": cards` to the returned dict (alongside `"spend": spend_summary`):

```python
@app.get("/health")
async def health():
    cards = _rate_cards_snapshot_and_maybe_refresh()
    async with _spend_lock:
        spend_summary = {
            "total_usd": round(_spend_total_usd, 8),
            "requests": _spend_requests,
            "since": _spend_since,
        }
    return {
        "status": "ok",
        "local": LOCAL_BASE_URL,
        "cloud": CLOUD_BASE_URL,
        "local_models": sorted(LOCAL_MODELS),
        "local_context_limit": LOCAL_CONTEXT_LIMIT,
        "local_connect_timeout": LOCAL_CONNECT_TIMEOUT,
        "vision": {
            "enabled": bool(VISION_SHIM_MODEL or VISION_CLOUD_MODEL),
            "mode": VISION_MODE,
            "local_ocr_model": VISION_SHIM_MODEL,
            "local_ocr_url": VISION_SHIM_URL,
            "cloud_model": VISION_CLOUD_MODEL,
            "ocr_min_chars": VISION_OCR_MIN_CHARS,
            "vision_capable_models": sorted(VISION_CAPABLE_MODELS),
        },
        "rate_cards": cards,
        "spend": spend_summary,
    }
```

- [ ] **Step 7: Run the full suite + import check**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q && /Users/brendanl/.venvs/mlx/bin/python3 -c "import llm_router; print('import ok')"`
Expected: all tests PASS; `import ok`

- [ ] **Step 8: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: live rate card from OpenRouter pricing on /health and /v1/spend"
```

---

### Task 5: Advertise virtual models on `/v1/models`

**Files:**
- Modify: `llm_router.py` — add `_virtual_model_entries`; prepend it in the `models` endpoint (~line 683)
- Test: `test_routing.py`

**Interfaces:**
- Consumes: `VIRTUAL_MODELS` (Task 1).
- Produces: `_virtual_model_entries() -> list[dict]` — one `{"id", "object": "model", "owned_by": "airwolf-llm-router", "context_length"}` per registry entry.

- [ ] **Step 1: Write the failing test** — append to `test_routing.py` (uses the fixture's patched registry):

```python
def test_virtual_model_entries_shape():
    entries = R._virtual_model_entries()
    assert any(e["id"] == "airwolf/auto" for e in entries)
    e = entries[0]
    assert e["object"] == "model"
    assert e["owned_by"] == "airwolf-llm-router"
    assert "context_length" in e
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k virtual_model_entries -q`
Expected: FAIL with `AttributeError: ... has no attribute '_virtual_model_entries'`

- [ ] **Step 3: Add the helper** just above the `models` endpoint (~line 678):

```python
def _virtual_model_entries() -> list[dict[str, Any]]:
    """Synthesized /v1/models entries advertising the router's virtual models."""
    return [
        {
            "id": vm.id,
            "object": "model",
            "owned_by": "airwolf-llm-router",
            "context_length": vm.advertised_context,
        }
        for vm in VIRTUAL_MODELS.values()
    ]
```

- [ ] **Step 4: Prepend in the `models` endpoint.** Change:

```python
    merged: list[dict[str, Any]] = []
```

to:

```python
    merged: list[dict[str, Any]] = _virtual_model_entries()
```

- [ ] **Step 5: Run tests to verify pass**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: PASS (all green)

- [ ] **Step 6: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: advertise virtual models on /v1/models"
```

---

### Task 6: Update client config, docstring, AGENTS.md + end-to-end verification

**Files:**
- Modify: `/Users/brendanl/.config/opencode/opencode-openrouter-and-local.json` (client config; live `opencode.json` is a symlink to it)
- Modify: `llm_router.py` docstring config section (~lines 29-59)
- Modify: `AGENTS.md`

**Interfaces:** none (docs + config only).

- [ ] **Step 1: Rewrite the OpenCode model block.** In `opencode-openrouter-and-local.json`, replace the `"z-ai/glm-5.2": {...}` entry with the honest virtual entry, keeping the local Qwen entry:

```jsonc
      "models": {
        "airwolf/auto": {
          "name": "Airwolf Auto (local→cloud, vision)",
          "limit": { "context": 1048576, "output": 32768 },
          "attachment": true,
          "modalities": { "input": ["text", "image"], "output": ["text"] }
        },
        "mlx-community/Qwen3.6-35B-A3B-4bit": {
          "name": "Qwen3.6-35B-A3B-4bit-MLX"
        }
      }
```

- [ ] **Step 2: Update the docstring config section** in `llm_router.py`. After the `CLOUD_DEFAULT_MODEL` line (~line 35) and before `LOCAL_CONNECT_TIMEOUT`, add documentation for the new env vars:

```
  AUTO_MODEL_ID          virtual model id clients send (default "airwolf/auto").
                         Resolved by the router to real upstream models below.
  AUTO_CLOUD_MODEL       cloud target the virtual model routes to (default
                         "z-ai/glm-5.2").
  AUTO_LOCAL_MODEL       local target for the virtual model; unset = first
                         LOCAL_MODELS entry.
  RATE_CARD_TTL          seconds before the live rate card is refreshed
                         (default 86400). Fetch is non-blocking and best-effort.
  RATE_CARD_URL          pricing source (default OpenRouter /api/v1/models).
```

- [ ] **Step 3: Update `AGENTS.md`.** Make three edits:

(a) Replace the opening sentence's tail "based on intent heuristics." and add a virtual-model note. After the intro paragraph add a new section:

```markdown
## Virtual models

The router owns a `VirtualModel` registry (`VIRTUAL_MODELS` in `llm_router.py`).
Clients send a single virtual id (default `airwolf/auto`); the router resolves it
to real upstream models via the routing rules below — `cloud_target`
(`z-ai/glm-5.2`) when routed to cloud, the first `LOCAL_MODELS` entry when local.
The virtual id is never forwarded upstream. This is why the OpenCode config
declares one honest `airwolf/auto` entry with `attachment: true` (true of the
pipeline — the router shims/reroutes images) instead of lying about a specific
model. Override ids via `AUTO_MODEL_ID` / `AUTO_CLOUD_MODEL` / `AUTO_LOCAL_MODEL`.
```

(b) In "Routing rules (first match wins)", add rule 0 at the top:

```markdown
0. `model` matches a `VIRTUAL_MODELS` id → resolve via rules below, substituting
   the real upstream id (`x-quality: best` or oversized prompt → `cloud_target`,
   else local target)
```

(c) Under "Cloud spend tracking", append a rate-card paragraph and fix the stale "No tests" line in the header and Verification section:

```markdown
The router also exposes a live **rate card** (price *before* spending) fetched
from OpenRouter pricing (`RATE_CARD_URL`, cached `RATE_CARD_TTL` seconds, lazy +
non-blocking). It appears under `rate_cards` in both `/health` and `/v1/spend`,
giving per-Mtok input/output/cache-read rates for each `cloud_target` alongside
the `usage.cost` actuals. On fetch, a text-only `cloud_target` whose virtual
model declares vision logs a one-line shim-required self-check.
```

Change the intro line "No build, no tests, no requirements file." to "No build step; a minimal `pytest` covers routing (`test_routing.py`); no requirements file." Change the Verification section's "No automated tests." to reference `test_routing.py`.

- [ ] **Step 4: Deploy + restart the router** (deployed file is a symlink to the repo copy):

Run: `launchctl kickstart -k gui/$(id -u)/com.local.llm-router`
Then wait ~2s and check it came up: `curl -s localhost:9090/health | python3 -m json.tool | head -40`
Expected: `"status": "ok"` and a `rate_cards` key (may be `{}` on the very first hit, then populated).

- [ ] **Step 5: End-to-end verification.** Run each and confirm:

```bash
# Rate card populates on a second call (first call schedules the fetch)
curl -s localhost:9090/health >/dev/null; sleep 2
curl -s localhost:9090/v1/spend | python3 -c 'import sys,json; d=json.load(sys.stdin); print("rate_cards:", json.dumps(d.get("rate_cards"), indent=2))'
# Expect z-ai/glm-5.2 with input_per_mtok ~1.0, output_per_mtok ~4.0

# Virtual model advertised
curl -s localhost:9090/v1/models | python3 -c 'import sys,json; print("airwolf/auto" in [m["id"] for m in json.load(sys.stdin)["data"]])'
# Expect: True

# Routing: small prompt to airwolf/auto -> LOCAL with substituted id (check out.log)
curl -s localhost:9090/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"airwolf/auto","messages":[{"role":"user","content":"say hi"}]}' >/dev/null
tail -5 ~/Library/Logs/llm-router/out.log
# Expect a line: -> http://localhost:7979/v1 model=mlx-community/Qwen3.6-35B-A3B-4bit reason=virtual-local

# Self-check log present
grep -i "vision shim required" ~/Library/Logs/llm-router/out.log | tail -1
# Expect: vision shim required for cloud_target z-ai/glm-5.2 (text-only)
```

- [ ] **Step 6: Commit** (repo files only; the OpenCode config lives outside the repo and is updated in place):

```bash
git add llm_router.py AGENTS.md
git commit -m "docs: document virtual models + rate card; refresh AGENTS.md and env docs"
```

- [ ] **Step 7: Final full-suite run**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: PASS (all green)

---

## Notes for the implementer

- **Line numbers are approximate** (the file evolves as you edit). Anchor on the named functions/blocks, not the numbers.
- **Run the suite after every task.** The characterization tests from the baseline commit are your regression net — if a pre-existing test goes red, you changed behavior you were supposed to preserve.
- **The OpenCode config is outside the git repo.** It won't show in `git status`; edit it in place and verify by restarting OpenCode (or just rely on the router-side curl checks).
- **Rate-card first-hit emptiness is expected** — the snapshot helper returns the cache and schedules the fetch; the second request sees populated cards.
