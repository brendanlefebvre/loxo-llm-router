# Agent Archetype Tiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add named virtual-model tiers (`airwolf/fast`, `airwolf/deep`, `airwolf/local`) alongside the existing `airwolf/auto`, with pinned routing + per-tier vision contracts, and wire matching OpenCode agent archetypes.

**Architecture:** Extend the `VirtualModel` dataclass with explicit `routing` and `vision` policy fields; branch `pick_target` on `routing`; suppress the local→cloud transport fallback for the pinned-local tier (hard-fail); enforce per-tier vision via `apply_vision_policy` (raising `VisionRejected` → HTTP 422). OpenCode declares the tiers as models and binds agents to them (asymmetric: cheap lanes may auto-invoke, the expensive lane is explicit-only).

**Tech Stack:** Python 3, FastAPI, httpx, pytest. Single-file router (`llm_router.py`). OpenCode config (JSON + markdown agent files) under `~/.config/opencode/`.

## Global Constraints

- Router is a single file: `/Users/brendanl/src/llm-router/llm_router.py`. The deployed copy `~/bin/llm_router.py` is a symlink to it; edits take effect on LaunchAgent restart.
- Run tests with the router venv: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q` (run from repo root `/Users/brendanl/src/llm-router`).
- Tests in `test_routing.py` are pure-function characterization tests — no network, no server.
- `airwolf/auto` behavior must remain byte-for-byte unchanged in effect (defaults `routing="auto"`, `vision="shim"`).
- All cloud traffic is OpenRouter; use OpenRouter model ids. Tier targets: `fast`=`z-ai/glm-4.7-flash`, `deep`=`google/gemini-2.5-pro`, `auto` keeps `z-ai/glm-5.2`.
- All ids/targets env-overridable: `FAST_MODEL_ID`/`FAST_CLOUD_MODEL`, `DEEP_MODEL_ID`/`DEEP_CLOUD_MODEL`, `LOCAL_TIER_MODEL_ID`/`LOCAL_TIER_MODEL` (local-tier prefix `LOCAL_TIER_` avoids collision with existing `LOCAL_*` vars).
- Work happens on branch `feature/agent-archetype-tiers` (already created); commit after each router task.
- OpenCode config files live OUTSIDE this repo (`~/.config/opencode/`) — those tasks save + verify, they are not llm-router commits.

---

### Task 1: VirtualModel policy fields + four-tier registry

**Files:**
- Modify: `/Users/brendanl/src/llm-router/llm_router.py:101-124` (dataclass + registry), and the module docstring env-var list (`:29-68`)
- Test: `/Users/brendanl/src/llm-router/test_routing.py`

**Interfaces:**
- Produces: `VirtualModel(id, cloud_target=None, local_target=None, routing="auto", vision="shim", advertised_context=1_048_576)`; `_build_virtual_models() -> dict[str, VirtualModel]`; `VIRTUAL_MODELS` registry with keys `airwolf/auto|fast|deep|local`.

- [ ] **Step 1: Write the failing test**

Add to `test_routing.py` (after `test_resolve_virtual_unknown_returns_none`):

```python
def test_default_registry_has_four_tiers_with_policies():
    reg = R._build_virtual_models()
    assert set(reg) >= {"airwolf/auto", "airwolf/fast", "airwolf/deep", "airwolf/local"}
    assert reg["airwolf/auto"].routing == "auto"
    assert reg["airwolf/auto"].vision == "shim"
    assert reg["airwolf/fast"].routing == "cloud"
    assert reg["airwolf/fast"].vision == "reject"
    assert reg["airwolf/fast"].cloud_target == "z-ai/glm-4.7-flash"
    assert reg["airwolf/deep"].routing == "cloud"
    assert reg["airwolf/deep"].vision == "native"
    assert reg["airwolf/deep"].cloud_target == "google/gemini-2.5-pro"
    assert reg["airwolf/local"].routing == "local"
    assert reg["airwolf/local"].vision == "local"
    assert reg["airwolf/local"].cloud_target is None
```

Also update the existing `test_resolve_virtual_known` — change its last assertion from `assert vm.vision is True` to:

```python
    assert vm.vision == "shim"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py::test_default_registry_has_four_tiers_with_policies -v`
Expected: FAIL with `AttributeError: module 'llm_router' has no attribute '_build_virtual_models'`

- [ ] **Step 3: Write minimal implementation**

Replace the dataclass field block (`llm_router.py:109-113`) so the fields read:

```python
    id: str
    cloud_target: str | None = None
    local_target: str | None = None
    routing: str = "auto"            # "auto" | "cloud" | "local"
    vision: str = "shim"             # "native" | "shim" | "local" | "reject"
    advertised_context: int = 1_048_576
```

Replace the registry block (`llm_router.py:116-124`) with a builder + module global:

```python
def _build_virtual_models() -> dict[str, "VirtualModel"]:
    return {vm.id: vm for vm in (
        VirtualModel(
            id=os.environ.get("AUTO_MODEL_ID", "airwolf/auto"),
            cloud_target=os.environ.get("AUTO_CLOUD_MODEL", "z-ai/glm-5.2"),
            local_target=os.environ.get("AUTO_LOCAL_MODEL") or None,
            routing="auto",
            vision="shim",
        ),
        VirtualModel(
            id=os.environ.get("FAST_MODEL_ID", "airwolf/fast"),
            cloud_target=os.environ.get("FAST_CLOUD_MODEL", "z-ai/glm-4.7-flash"),
            routing="cloud",
            vision="reject",
            advertised_context=202_752,
        ),
        VirtualModel(
            id=os.environ.get("DEEP_MODEL_ID", "airwolf/deep"),
            cloud_target=os.environ.get("DEEP_CLOUD_MODEL", "google/gemini-2.5-pro"),
            routing="cloud",
            vision="native",
            advertised_context=1_048_576,
        ),
        VirtualModel(
            id=os.environ.get("LOCAL_TIER_MODEL_ID", "airwolf/local"),
            cloud_target=None,
            local_target=os.environ.get("LOCAL_TIER_MODEL") or None,
            routing="local",
            vision="local",
        ),
    )}


VIRTUAL_MODELS: dict[str, VirtualModel] = _build_virtual_models()
```

Then add these lines to the module docstring env-var list (after the `AUTO_LOCAL_MODEL` entry, ~`:41`):

```
  FAST_MODEL_ID          virtual id for the pinned-cloud "fast" tier
                         (default "airwolf/fast").
  FAST_CLOUD_MODEL       cloud model the fast tier pins to
                         (default "z-ai/glm-4.7-flash").
  DEEP_MODEL_ID          virtual id for the pinned-cloud "deep" tier
                         (default "airwolf/deep").
  DEEP_CLOUD_MODEL       cloud model the deep tier pins to
                         (default "google/gemini-2.5-pro").
  LOCAL_TIER_MODEL_ID    virtual id for the pinned-local tier
                         (default "airwolf/local").
  LOCAL_TIER_MODEL       local model the local tier pins to; unset = first
                         LOCAL_MODELS entry.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: PASS (all tests, including the updated `test_resolve_virtual_known`)

- [ ] **Step 5: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: VirtualModel routing/vision policy fields + four-tier registry"
```

---

### Task 2: pick_target branches on routing policy

**Files:**
- Modify: `/Users/brendanl/src/llm-router/llm_router.py:606-612` (the `vm is not None` branch in `pick_target`)
- Test: `/Users/brendanl/src/llm-router/test_routing.py`

**Interfaces:**
- Consumes: `VirtualModel.routing`, `local_target_for(vm, model)` (existing).
- Produces: `pick_target` reasons `"virtual-pinned-cloud"`, `"virtual-pinned-local"` (plus unchanged `"virtual-quality-best"`, `"virtual-prompt-too-long"`, `"virtual-local"`).

- [ ] **Step 1: Write the failing test**

Add to `test_routing.py`:

```python
def test_pinned_cloud_tier_always_cloud(monkeypatch):
    monkeypatch.setitem(R.VIRTUAL_MODELS, "airwolf/fast", R.VirtualModel(
        id="airwolf/fast", cloud_target="z-ai/glm-4.7-flash", routing="cloud", vision="reject"))
    base, model, reason = R.pick_target(_body(model="airwolf/fast", text="hi"), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-4.7-flash"  # virtual id NOT forwarded
    assert reason == "virtual-pinned-cloud"
    # pinned: size and quality headers do not change the lane
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 1)
    base2, model2, reason2 = R.pick_target(_body(model="airwolf/fast", text="x" * 1000), "best")
    assert (base2, model2, reason2) == (R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", "virtual-pinned-cloud")


def test_pinned_local_tier_always_local(monkeypatch):
    monkeypatch.setitem(R.VIRTUAL_MODELS, "airwolf/local", R.VirtualModel(
        id="airwolf/local", cloud_target=None, routing="local", vision="local"))
    # even with x-quality: best and a huge prompt, it stays local
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 1)
    base, model, reason = R.pick_target(_body(model="airwolf/local", text="x" * 1000), "best")
    assert base == R.LOCAL_BASE_URL
    assert model == "mlx-community/Qwen3.6-35B-A3B-4bit"  # first LOCAL_MODELS entry
    assert reason == "virtual-pinned-local"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py::test_pinned_cloud_tier_always_cloud test_routing.py::test_pinned_local_tier_always_local -v`
Expected: FAIL — `airwolf/fast` currently hits the `auto` logic, so reason is `virtual-local`, not `virtual-pinned-cloud`.

- [ ] **Step 3: Write minimal implementation**

Replace the `vm is not None` block in `pick_target` (`llm_router.py:606-612`) with:

```python
    vm = resolve_virtual(model)
    if vm is not None:
        if vm.routing == "cloud":
            return CLOUD_BASE_URL, vm.cloud_target, "virtual-pinned-cloud"
        if vm.routing == "local":
            return LOCAL_BASE_URL, local_target_for(vm, model), "virtual-pinned-local"
        # routing == "auto"
        if (quality_header or "").lower() == "best":
            return CLOUD_BASE_URL, vm.cloud_target, "virtual-quality-best"
        if estimate_prompt_tokens(body) > LOCAL_CONTEXT_LIMIT:
            return CLOUD_BASE_URL, vm.cloud_target, "virtual-prompt-too-long"
        return LOCAL_BASE_URL, local_target_for(vm, model), "virtual-local"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: PASS (new pinned tests + all existing `virtual-*` regression tests)

- [ ] **Step 5: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: pin routing for cloud/local tiers in pick_target"
```

---

### Task 3: Suppress cloud fallback for the pinned-local tier (hard-fail)

**Files:**
- Modify: `/Users/brendanl/src/llm-router/llm_router.py` — add `cloud_fallback_for` helper near `pick_target` (after `:626`); rewire the fallback block in `chat_completions` (`:814-824`)
- Test: `/Users/brendanl/src/llm-router/test_routing.py`

**Interfaces:**
- Produces: `cloud_fallback_for(base_url: str, vm: VirtualModel | None) -> bool` — True only when a LOCAL request is allowed to transparently fall back to cloud (False for the pinned-local tier and for any cloud-base request).

- [ ] **Step 1: Write the failing test**

Add to `test_routing.py`:

```python
def test_cloud_fallback_allowed_for_auto_local():
    vm = R.VirtualModel(id="airwolf/auto", cloud_target="z-ai/glm-5.2", routing="auto")
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, vm) is True


def test_cloud_fallback_allowed_for_nonvirtual_local():
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, None) is True


def test_cloud_fallback_suppressed_for_local_pin():
    vm = R.VirtualModel(id="airwolf/local", routing="local", vision="local")
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, vm) is False


def test_cloud_fallback_none_when_base_is_cloud():
    assert R.cloud_fallback_for(R.CLOUD_BASE_URL, None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k cloud_fallback -v`
Expected: FAIL with `AttributeError: module 'llm_router' has no attribute 'cloud_fallback_for'`

- [ ] **Step 3: Write minimal implementation**

Add this helper immediately after `pick_target` (after `llm_router.py:626`):

```python
def cloud_fallback_for(base_url: str, vm: "VirtualModel | None") -> bool:
    """Whether a LOCAL-bound request may transparently fall back to cloud on a
    transport failure. Suppressed for the pinned-local tier so that an oversized
    prompt or a down local server hard-fails instead of silently spending cloud."""
    if base_url != LOCAL_BASE_URL:
        return False
    if vm is not None and vm.routing == "local":
        return False
    return True
```

Then replace the fallback block in `chat_completions` (`llm_router.py:814-824`) with:

```python
    # Only LOCAL gets a cloud fallback; the pinned-local tier opts out (hard-fail).
    fallback_url: str | None = None
    fallback_body: bytes | None = None
    fallback_cloud_model = (requested_vm.cloud_target if requested_vm else None) or CLOUD_DEFAULT_MODEL
    if cloud_fallback_for(base_url, requested_vm):
        fb = dict(body)
        fb["model"] = fallback_cloud_model
        if stream:
            fb["stream_options"] = {**fb.get("stream_options", {}), "include_usage": True}
        fallback_url = CLOUD_BASE_URL
        fallback_body = json.dumps(fb).encode()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: hard-fail pinned-local tier by suppressing cloud fallback"
```

---

### Task 4: Per-tier vision policy + VisionRejected → HTTP 422

**Files:**
- Modify: `/Users/brendanl/src/llm-router/llm_router.py` — add `JSONResponse` import (`:90`); add `VisionRejected` + rewrite `apply_vision_policy` (`:527-586`); update the call site + add try/except in `chat_completions` (`:789-792`)
- Test: `/Users/brendanl/src/llm-router/test_routing.py`

**Interfaces:**
- Consumes: `count_images`, `apply_vision_shim`, `is_vision_capable`, `VISION_SHIM_MODEL`, `VISION_CLOUD_MODEL`, `VISION_MODE`, `VISION_OCR_MIN_CHARS` (existing).
- Produces: `class VisionRejected(Exception)` with `.message`; `apply_vision_policy(..., vision_policy: str = "shim")` — raises `VisionRejected` for `reject`/`local` hard-fail paths.

Note: `native` is handled by an early return here, so NO change to `VISION_CAPABLE_MODELS` is needed for the deep tier (improvement over the spec prose).

- [ ] **Step 1: Write the failing tests**

Replace the existing `test_vision_policy_disabled_short_circuits` (it used the removed `vision_enabled` arg) with these:

```python
def _img_body():
    return {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]}


def test_vision_policy_native_passes_through():
    body = _img_body()
    out = asyncio.run(R.apply_vision_policy(
        body, R.CLOUD_BASE_URL, "google/gemini-2.5-pro", "virtual-pinned-cloud",
        None, vision_policy="native"))
    assert out == (R.CLOUD_BASE_URL, "google/gemini-2.5-pro", body, "virtual-pinned-cloud")


def test_vision_policy_reject_raises_on_image():
    with pytest.raises(R.VisionRejected):
        asyncio.run(R.apply_vision_policy(
            _img_body(), R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", "virtual-pinned-cloud",
            None, vision_policy="reject"))


def test_vision_policy_reject_passes_through_without_image():
    body = {"messages": [{"role": "user", "content": "just text"}]}
    out = asyncio.run(R.apply_vision_policy(
        body, R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", "r", None, vision_policy="reject"))
    assert out == (R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", body, "r")


def test_vision_policy_local_no_shim_raises(monkeypatch):
    monkeypatch.setattr(R, "VISION_SHIM_MODEL", "")
    with pytest.raises(R.VisionRejected):
        asyncio.run(R.apply_vision_policy(
            _img_body(), R.LOCAL_BASE_URL, "qwen-local", "virtual-pinned-local",
            None, vision_policy="local"))


def test_vision_policy_local_thin_ocr_raises(monkeypatch):
    monkeypatch.setattr(R, "VISION_SHIM_MODEL", "some-vlm")
    monkeypatch.setattr(R, "VISION_OCR_MIN_CHARS", 50)

    async def fake_shim(body):
        return ({"shimmed": True}, 3)  # thin
    monkeypatch.setattr(R, "apply_vision_shim", fake_shim)
    with pytest.raises(R.VisionRejected):
        asyncio.run(R.apply_vision_policy(
            _img_body(), R.LOCAL_BASE_URL, "qwen-local", "virtual-pinned-local",
            None, vision_policy="local"))


def test_vision_policy_local_rich_ocr_feeds_text(monkeypatch):
    monkeypatch.setattr(R, "VISION_SHIM_MODEL", "some-vlm")
    monkeypatch.setattr(R, "VISION_OCR_MIN_CHARS", 10)

    async def fake_shim(body):
        return ({"shimmed": True}, 200)  # rich
    monkeypatch.setattr(R, "apply_vision_shim", fake_shim)
    base, model, new_body, reason = asyncio.run(R.apply_vision_policy(
        _img_body(), R.LOCAL_BASE_URL, "qwen-local", "virtual-pinned-local",
        None, vision_policy="local"))
    assert base == R.LOCAL_BASE_URL
    assert new_body == {"shimmed": True}
    assert reason.endswith("vision-local-pin")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -k vision_policy -v`
Expected: FAIL — `apply_vision_policy` has no `vision_policy` kwarg / `R.VisionRejected` undefined.

- [ ] **Step 3: Write minimal implementation**

3a. Update the import (`llm_router.py:90`):

```python
from fastapi.responses import JSONResponse, StreamingResponse
```

3b. Add the exception immediately before `async def apply_vision_policy` (`:527`):

```python
class VisionRejected(Exception):
    """Image content hit a tier whose vision policy forbids serving it. The caller
    turns this into an HTTP 422 with .message so the user can switch tiers."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)
```

3c. Replace `apply_vision_policy`'s signature and its `if not vision_enabled: ...` prologue (`:533` and `:548-555`) with the new signature + policy dispatch. The full function body becomes:

```python
async def apply_vision_policy(
    body: dict[str, Any],
    base_url: str,
    model_to_send: str,
    reason: str,
    x_vision: str | None,
    vision_policy: str = "shim",
) -> tuple[str, str, dict[str, Any], str]:
    """Handle image content per the tier's vision policy.

      native - target sees images itself; pass through untouched
      shim   - local OCR, may escalate to cloud if thin (VISION_MODE / x-vision)
      local  - on-machine OCR only; raise VisionRejected if thin or no shim;
               never escalates to cloud (would break the local pin)
      reject - any image content -> raise VisionRejected
    """
    has_images = bool(count_images(body))

    if vision_policy == "native":
        return base_url, model_to_send, body, reason

    if vision_policy == "reject":
        if has_images:
            raise VisionRejected(
                "this model tier has no vision; switch to airwolf/auto or airwolf/deep"
            )
        return base_url, model_to_send, body, reason

    if vision_policy == "local":
        if not has_images:
            return base_url, model_to_send, body, reason
        if not VISION_SHIM_MODEL:
            raise VisionRejected(
                "airwolf/local has no local OCR configured (VISION_SHIM_MODEL unset); "
                "switch to airwolf/deep for vision"
            )
        shimmed, n_chars = await apply_vision_shim(body)
        if n_chars < VISION_OCR_MIN_CHARS:
            raise VisionRejected(
                "image isn't text-readable locally; switch to airwolf/deep for native vision"
            )
        log(f"[router] vision=local-pin: OCR'd image(s) -> {n_chars} chars for {model_to_send}")
        return base_url, model_to_send, shimmed, f"{reason}+vision-local-pin"

    # vision_policy == "shim" (default): existing behavior, unchanged.
    if is_vision_capable(model_to_send):
        return base_url, model_to_send, body, reason
    if not has_images:
        return base_url, model_to_send, body, reason
    if not (VISION_SHIM_MODEL or VISION_CLOUD_MODEL):
        return base_url, model_to_send, body, reason

    mode = (x_vision or VISION_MODE or "auto").strip().lower()
    if mode not in {"local", "cloud", "auto"}:
        mode = "auto"

    def _reroute_to_cloud(why: str) -> tuple[str, str, dict[str, Any], str]:
        b = dict(body)
        b["model"] = VISION_CLOUD_MODEL
        log(f"[router] vision={why}: reroute image request -> cloud/{VISION_CLOUD_MODEL}")
        return CLOUD_BASE_URL, VISION_CLOUD_MODEL, b, f"vision-{why}"

    if mode == "cloud" or not VISION_SHIM_MODEL:
        if VISION_CLOUD_MODEL:
            return _reroute_to_cloud("cloud")
        log("[router] vision=cloud requested but VISION_CLOUD_MODEL unset; image passes through")
        return base_url, model_to_send, body, reason

    shimmed, n_chars = await apply_vision_shim(body)
    if mode == "local":
        log(f"[router] vision=local: OCR'd image(s) -> {n_chars} chars for {model_to_send}")
        return base_url, model_to_send, shimmed, f"{reason}+vision-local"
    if n_chars >= VISION_OCR_MIN_CHARS or not VISION_CLOUD_MODEL:
        log(f"[router] vision=auto: OCR {n_chars} chars -> local text for {model_to_send}")
        return base_url, model_to_send, shimmed, f"{reason}+vision-auto-local"
    log(f"[router] vision=auto: OCR thin ({n_chars} < {VISION_OCR_MIN_CHARS}) -> escalating to cloud")
    return _reroute_to_cloud("auto-escalated")
```

3d. Update the call site in `chat_completions` (`:789-792`) to pass the policy and catch the rejection:

```python
    try:
        base_url, model_to_send, body, reason = await apply_vision_policy(
            body, base_url, model_to_send, reason, x_vision,
            vision_policy=(requested_vm.vision if requested_vm else "shim"),
        )
    except VisionRejected as e:
        return JSONResponse(
            status_code=422,
            content={"error": {"message": e.message,
                               "type": "invalid_request_error",
                               "code": "vision_unsupported"}},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: PASS (all vision-policy tests + full suite)

- [ ] **Step 5: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "feat: per-tier vision policy with VisionRejected -> HTTP 422"
```

---

### Task 5: Rate-card None-target filter + self-check fix

**Files:**
- Modify: `/Users/brendanl/src/llm-router/llm_router.py` — add `_distinct_cloud_targets` near the rate-card code (after `:276`); use it in `get_rate_cards` (`:285`); fix self-check condition (`:304`)
- Test: `/Users/brendanl/src/llm-router/test_routing.py`

**Interfaces:**
- Produces: `_distinct_cloud_targets() -> set[str]` — every non-None `cloud_target` in the registry.

- [ ] **Step 1: Write the failing test**

Add to `test_routing.py`:

```python
def test_distinct_cloud_targets_excludes_none(monkeypatch):
    monkeypatch.setattr(R, "VIRTUAL_MODELS", {
        "a": R.VirtualModel(id="a", cloud_target="z-ai/glm-5.2", routing="auto"),
        "f": R.VirtualModel(id="f", cloud_target="z-ai/glm-4.7-flash", routing="cloud"),
        "l": R.VirtualModel(id="l", cloud_target=None, routing="local"),
    })
    assert R._distinct_cloud_targets() == {"z-ai/glm-5.2", "z-ai/glm-4.7-flash"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py::test_distinct_cloud_targets_excludes_none -v`
Expected: FAIL with `AttributeError: module 'llm_router' has no attribute '_distinct_cloud_targets'`

- [ ] **Step 3: Write minimal implementation**

Add after `_parse_rate_card` (after `llm_router.py:276`):

```python
def _distinct_cloud_targets() -> set[str]:
    """Every non-None cloud_target in the registry (local-pinned tiers have none)."""
    return {vm.cloud_target for vm in VIRTUAL_MODELS.values() if vm.cloud_target}
```

In `get_rate_cards`, replace the `targets = {...}` line (`:285`) with:

```python
        targets = _distinct_cloud_targets()
```

Fix the self-check condition (`:304`) so a string vision policy is interpreted correctly — only the `shim` policy genuinely needs the shim for a text-only cloud target:

```python
                if vm.cloud_target == t and vm.vision == "shim" and card.get("input_modalities") == ["text"]:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add llm_router.py test_routing.py
git commit -m "fix: rate card skips None cloud_target; self-check honors string vision policy"
```

---

### Task 6: Update AGENTS.md with the tier roster

**Files:**
- Modify: `/Users/brendanl/src/llm-router/AGENTS.md` (the `## Virtual models` and `## Routing rules` sections, ~`:29-54`)

**Interfaces:** none (documentation).

- [ ] **Step 1: Update the Virtual models section**

Append after the existing `## Virtual models` paragraph (`AGENTS.md:38`):

```markdown

### Tiers (archetypes)

Beyond the default `airwolf/auto`, the registry advertises three pinned tiers so
client agents can pick a cost/quality lane per task:

| Tier | routing | target | vision |
|---|---|---|---|
| `airwolf/auto` | local-first, escalate on size/`best` | local → `z-ai/glm-5.2` | shim |
| `airwolf/fast` | pinned cloud | `z-ai/glm-4.7-flash` | reject images (422) |
| `airwolf/deep` | pinned cloud | `google/gemini-2.5-pro` | native |
| `airwolf/local` | pinned local, **hard-fail** (no cloud fallback) | first `LOCAL_MODELS` | local OCR only, else 422 |

Pinned-local hard-fails (oversized prompt, local down, or unreadable image)
return HTTP 422 with a message naming the remedy — switch to `airwolf/auto` or
`airwolf/deep`. Override ids/targets via `FAST_*`, `DEEP_*`, `LOCAL_TIER_*` env
vars (see the `llm_router.py` docstring).
```

Update the routing-rules list (`AGENTS.md:42-44`, rule 0) to note pinned routing:

```markdown
0. `model` matches a `VIRTUAL_MODELS` id → resolve by its `routing` policy:
   `cloud` (pinned cloud target), `local` (pinned local, no cloud fallback), or
   `auto` (`x-quality: best` or oversized prompt → `cloud_target`, else local).
```

- [ ] **Step 2: Verify rendering**

Run: `sed -n '29,75p' AGENTS.md`
Expected: the new tier table and updated rule 0 appear, well-formed.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: document archetype tiers in AGENTS.md"
```

---

### Task 7: OpenCode model entries for the new tiers

**Files:**
- Modify: `~/.config/opencode/opencode-openrouter-and-local.json` (the active config target of the `opencode.json` symlink)

**Interfaces:** none (consumed by OpenCode, not the router). NOT an llm-router repo commit.

- [ ] **Step 1: Add the three model entries**

In `provider.airwolf-llm-router.models`, alongside `airwolf/auto`, add:

```json
        "airwolf/fast": {
          "name": "Airwolf Fast (cloud, cheap)",
          "limit": { "context": 202752, "output": 32768 },
          "modalities": { "input": ["text"], "output": ["text"] }
        },
        "airwolf/deep": {
          "name": "Airwolf Deep (cloud, top reasoning)",
          "limit": { "context": 1048576, "output": 32768 },
          "attachment": true,
          "modalities": { "input": ["text", "image"], "output": ["text"] }
        },
        "airwolf/local": {
          "name": "Airwolf Local (pinned local, no cloud)",
          "limit": { "context": 60000, "output": 32768 },
          "modalities": { "input": ["text"], "output": ["text"] }
        }
```

(`airwolf/local` context is set to the router's local budget `60000`; raise it only if the MLX serve window is larger.)

- [ ] **Step 2: Verify the JSON is valid**

Run: `python3 -c "import json,os; json.load(open(os.path.expanduser('~/.config/opencode/opencode-openrouter-and-local.json'))); print('valid')"`
Expected: `valid`

- [ ] **Step 3: Verify the router advertises the tiers**

Run (router must be running): `curl -s -H "Authorization: Bearer $ROUTER_TOKEN" http://localhost:9090/v1/models | python3 -c "import sys,json; ids=[m['id'] for m in json.load(sys.stdin)['data']]; print([i for i in ids if i.startswith('airwolf/')])"`
Expected: list includes `airwolf/auto`, `airwolf/fast`, `airwolf/deep`, `airwolf/local`.

---

### Task 8: OpenCode agent archetypes (asymmetric: auto-down / explicit-up)

**Files:**
- Create: `~/.config/opencode/agents/plan-executor.md`
- Create: `~/.config/opencode/agents/architect.md`
- Create: `~/.config/opencode/agents/local.md`

**Interfaces:** none (consumed by OpenCode). NOT an llm-router repo commit.

- [ ] **Step 1: Create the directory**

Run: `mkdir -p ~/.config/opencode/agents`

- [ ] **Step 2: Write the plan-executor subagent (auto-invoked, fast)**

Create `~/.config/opencode/agents/plan-executor.md`:

```markdown
---
description: Mechanically executes the steps of an already-written implementation plan — applies edits, runs tests, and commits exactly as specified. Use for well-specified, low-ambiguity execution work. NOT for design, architecture, or open-ended debugging.
mode: subagent
model: airwolf-llm-router/airwolf/fast
temperature: 0.1
---

You execute pre-written plans precisely and economically. Follow the plan's steps
verbatim: make the specified edit, run the specified command, confirm the expected
output, then move on. Do not redesign, do not add scope, do not refactor opportunistically.
If a step is ambiguous or its expected output does not occur, stop and report rather than guess.
```

- [ ] **Step 3: Write the architect primary (explicit-only, deep)**

Create `~/.config/opencode/agents/architect.md`:

```markdown
---
description: Deep reasoning for architecture, system design, and hard debugging. Invoke explicitly (Tab to it, or @architect) — never auto-escalated.
mode: primary
model: airwolf-llm-router/airwolf/deep
---

You are the deep-reasoning archetype. Use the extra capability for genuinely hard
problems: architecture trade-offs, subtle bugs, and design. Think rigorously, weigh
alternatives, and recommend. You are reached only on deliberate request, so the work
in front of you is expected to warrant the cost.
```

- [ ] **Step 4: Write the local primary (explicit, pinned local) with recovery guidance**

Create `~/.config/opencode/agents/local.md` (the prompt mirrors the spec §5 recovery instruction so it stays discoverable even if OpenCode hides the 422 body):

```markdown
---
description: Offline / private / zero-cost local model. No cloud, ever. Switch to this for private or no-spend work.
mode: primary
model: airwolf-llm-router/airwolf/local
---

You run entirely on the local model — no request ever leaves the machine and no
cloud cost is incurred.

This tier hard-fails by design. If a request errors because the prompt is too large
for local context, the local server is down, or an image is not text-readable, the
fix is to switch tiers and resend: Tab to the default (airwolf/auto, which can use
cloud) or to airwolf/deep (for large context or native vision). The local tier will
never silently spend cloud money on your behalf.
```

- [ ] **Step 5: Verify OpenCode loads the agents**

Restart/reopen OpenCode and confirm `plan-executor`, `architect`, and `local` appear (e.g. via the agent switcher / `@`-mention list).
Expected: all three present, bound to their tiers.

---

### Task 9: Deploy, manual verification, and superpowers-coupling validation

**Files:** none (operational).

**Interfaces:** none.

- [ ] **Step 1: Run the full test suite**

Run: `/Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q`
Expected: all green.

- [ ] **Step 2: Restart the router to deploy**

Run: `launchctl kickstart -k gui/$(id -u)/com.local.llm-router`
Then: `tail -n 20 ~/Library/Logs/llm-router/err.log`
Expected: no traceback on startup.

- [ ] **Step 3: Verify pinned-cloud routing (fast)**

Send a tiny chat to `airwolf/fast` and check the routing log:

```bash
curl -s -H "Authorization: Bearer $ROUTER_TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"airwolf/fast","messages":[{"role":"user","content":"say hi"}]}' \
  http://localhost:9090/v1/chat/completions >/dev/null
grep "reason=virtual-pinned-cloud" ~/Library/Logs/llm-router/out.log | tail -1
```
Expected: a line with `model=z-ai/glm-4.7-flash reason=virtual-pinned-cloud`.

- [ ] **Step 4: Verify vision reject (fast) returns 422**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $ROUTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"airwolf/fast","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}}]}]}' \
  http://localhost:9090/v1/chat/completions
```
Expected: `422`.

- [ ] **Step 5: Verify pinned-local hard-fail (oversized) does not spend cloud**

Send an oversized prompt to `airwolf/local` (exceeds `LOCAL_CONTEXT_LIMIT`) and confirm it errors locally without a cloud fallback line. Then check `/v1/spend` is unchanged for the local attempt.

```bash
curl -s -H "Authorization: Bearer $ROUTER_TOKEN" http://localhost:9090/v1/spend | python3 -m json.tool | grep total_usd
```
Expected: no new cloud spend attributable to the local-tier request; `out.log` shows `reason=virtual-pinned-local` and no fallback.

- [ ] **Step 6: Validate superpowers dispatch coupling (Open risk)**

Run a real superpowers plan execution (in OpenCode) on a throwaway task, then:

```bash
curl -s -H "Authorization: Bearer $ROUTER_TOKEN" http://localhost:9090/v1/spend \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('by_provider',{}), indent=2))"
```
Expected/decision: confirm whether `z-ai/glm-4.7-flash` is billing for the execution work.
- If yes: auto-invocation works; done.
- If no: fall back to explicit `@plan-executor`, or switch to `airwolf/fast` as a primary during execution. Document the outcome in the spec's Open-risk section.

- [ ] **Step 7: Final confirmation**

State plainly which verifications passed (with the observed output) and which, if any, need follow-up. Do not claim success for steps not actually run.

---

## Self-Review

**Spec coverage:**
- §1 VirtualModel data model → Task 1 ✓
- §2 Registry (four entries, env overrides) → Task 1 ✓
- §3 Routing logic (cloud/local/auto) → Task 2 ✓
- §4 Hard-fail for local pin → Task 3 ✓
- §5 Recovery (422 messages name remedy; mirror in local agent prompt) → Task 4 (messages) + Task 8 step 4 (agent prompt) ✓; OpenCode-surfaces-422 validation → Task 9 step 4 surfaces the code; full TUI-body check is operational (noted in §5)
- §6 Vision contract per tier → Task 4 ✓ (native via early-return supersedes the VISION_CAPABLE_MODELS step — noted)
- §7 /v1/models advertisement → automatic; verified in Task 7 step 3 ✓
- OpenCode model entries → Task 7 ✓
- Archetypes (asymmetric) → Task 8 ✓
- Open risk (superpowers coupling) → Task 9 step 6 ✓
- Testing (pinned cloud/local, no-fallback, auto unchanged, vision reject/local) → Tasks 2,3,4 ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every command shows expected output.

**Type consistency:** `routing`/`vision` are `str` throughout; `cloud_target: str | None`; `cloud_fallback_for(base_url, vm) -> bool`; `_distinct_cloud_targets() -> set[str]`; `apply_vision_policy(..., vision_policy="shim")`; `VisionRejected.message`. Names match across tasks.

**Note on one spec deviation:** the spec's "add `gemini-2.5-pro` to `VISION_CAPABLE_MODELS`" is intentionally NOT done — the `native` policy early-return in `apply_vision_policy` (Task 4) makes it unnecessary and self-contained.
```
