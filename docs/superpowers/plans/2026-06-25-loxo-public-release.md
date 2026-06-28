# loxo-llm-router Public Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize this single-developer, Mac-specific `llm-router` into a cleanly branded, cross-platform, installable open-source project (`loxo-llm-router`) — with a deliberate **TOML-config + env-secrets** model — whose working tree can be snapshotted as the first commit of a public repo.

**Architecture:** Promote the single module to a package `loxo_llm_router/`. Configuration layers **bundled defaults < `loxo.toml` file < environment overrides**, with **secrets (API key, token) supplied only via env**. The tier catalog (6 tiers) lives in TOML, replacing the ~18 per-tier env vars. The router core is already backend-agnostic; the remaining work is the config layer, packaging, containerization, docs, and hygiene.

**Tech Stack:** Python ≥3.11 (stdlib `tomllib`), FastAPI, uvicorn[standard], httpx, setuptools/pyproject, Docker. pytest for routing + config tests.

## Global Constraints

- **Name:** distribution = `loxo-llm-router`; import package = `loxo_llm_router`; default tier namespace = `loxo`; `owned_by` field = `loxo-llm-router`.
- **Python floor:** `>=3.11` (so `tomllib` is stdlib — no TOML dependency).
- **Config model (precedence, low→high):** (1) bundled `loxo.default.toml`, (2) user `loxo.toml`, (3) environment variables.
- **Secrets are env-only:** `OPENROUTER_API_KEY`, `ROUTER_TOKEN` are never read from TOML.
- **Env override scalars (kept):** `ROUTER_NS`, `LOXO_HOST`/`HOST`, `LOXO_PORT`/`PORT`, `LOCAL_BASE_URL`, `CLOUD_BASE_URL`, `LOCAL_MODELS`, `LOCAL_CONTEXT_LIMIT`, `CLOUD_DEFAULT_MODEL`, `LOG_CONFIG`, `ROUTER_QUIET`, `LOXO_CONFIG`.
- **Bind-address resolution:** host/port resolve `LOXO_HOST`/`LOXO_PORT` (namespaced, wins) → bare `HOST`/`PORT` (12-factor / PaaS convention) → `[server]` TOML → built-in default. The bare names honor the Heroku/Cloud Run/Railway convention of injecting `PORT`; the namespaced names are an escape hatch that beats a stray *inherited* `PORT`.
- **Container-first deployment:** Docker/OCI is the documented default. A container's environment is hermetic and explicitly declared, so reading the bare `HOST`/`PORT` carries no accidental-capture risk there; the `LOXO_*` overrides cover the bare-metal / systemd / launchd runs where a shared environment could otherwise leak a conflicting value.
- **Retired env vars:** all per-tier ids/targets (`AUTO_MODEL_ID`, `AUTO_CLOUD_MODEL`, `AUTO_LOCAL_MODEL`, `FAST_MODEL_ID`, `FAST_CLOUD_MODEL`, `DEEP_*`, `BALANCED_*`, `REASON_*`, `LOCAL_TIER_*`). The `[tiers.*]` TOML table replaces them.
- **Config file search order:** `$LOXO_CONFIG` → `./loxo.toml` → `$XDG_CONFIG_HOME/loxo-llm-router/loxo.toml` (default `~/.config/...`) → bundled defaults only.
- **Runs with zero config** (only the API-key env needed for cloud): bundled defaults must reproduce today's behavior.
- **No new runtime dependencies** beyond `fastapi`, `uvicorn[standard]`, `httpx`.
- **License:** MIT, © 2026 Brendan LeFebvre.
- **Logging defaults to stdout/stderr;** UTC formatting available via `LOG_CONFIG`. No OS-specific log path in code.
- **Shipping files must contain zero occurrences of** `airwolf`, `/Users/brendanl`, `mac-mini`, `.venvs`, `Library/Logs`. (`docs/superpowers/` is internal, excluded from the public snapshot, exempt.)
- **GitHub owner for URLs:** `BrendanL79`.

---

### Task 1: Promote to a package (`loxo_llm_router/`)

Pure structural move — no behavior change — so later tasks edit a package, not a loose module. Tests stay green throughout.

**Files:**
- Create dir: `loxo_llm_router/`
- Rename: `llm_router.py` → `loxo_llm_router/__init__.py`
- Modify: `test_routing.py` (import line)

**Interfaces:**
- Produces: importable package `loxo_llm_router` exposing everything the current `llm_router` module did (`app`, `VirtualModel`, `VIRTUAL_MODELS`, `resolve_virtual`, `pick_target`, `local_target_for`, etc.).

- [ ] **Step 1: Create the package and move the module**

```bash
mkdir -p loxo_llm_router
git mv llm_router.py loxo_llm_router/__init__.py
```

- [ ] **Step 2: Update the test import**

In `test_routing.py`, change the import (currently `import llm_router as R`) to:

```python
import loxo_llm_router as R
```

- [ ] **Step 3: Run the suite to confirm the move is behavior-neutral**

Run: `python3 -m pytest -q`
Expected: all tests PASS (same count as before the move). If `ModuleNotFoundError: loxo_llm_router`, ensure you run pytest from the repo root.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: promote llm_router module to loxo_llm_router package"
```

---

### Task 2: Config loader — `config.py` + bundled `loxo.default.toml` (TDD)

Build the layered config loader in isolation (not yet wired into the app). This is the core of the new design, and precedence is highly testable.

**Files:**
- Create: `loxo_llm_router/config.py`
- Create: `loxo_llm_router/loxo.default.toml`
- Create: `test_config.py`

**Interfaces:**
- Produces: `VirtualModel` dataclass — fields `id: str`, `routing: str` (`"auto"|"cloud"|"local"`), `cloud_target: str | None`, `local_target: str | None`, `vision: str` (`"shim"|"native"|"reject"|"local"`), `advertised_context: int` (surfaced as `context_length` in `/v1/models`).
- Produces: `Config` dataclass — `namespace, host, port, local_base_url, cloud_base_url, local_models (tuple[str,...]), local_context_limit, cloud_default_model, tiers (dict[str, VirtualModel] keyed by full id), openrouter_api_key, router_token, quiet`.
- Produces: `load_config() -> Config` — applies defaults < `loxo.toml` < env; secrets env-only; tier id = `f"{namespace}/{table_key}"`.

> Note: `VirtualModel` moves here (from `__init__.py`) to avoid a circular import; Task 3 re-exports it so `R.VirtualModel` keeps working.

- [ ] **Step 1: Write the bundled defaults `loxo_llm_router/loxo.default.toml`**

Transcribe the **current** registry values verbatim from `loxo_llm_router/__init__.py` (the `VIRTUAL_MODELS` block). The known-good values:

```toml
namespace = "loxo"

[server]
host = "0.0.0.0"
port = 9090

[backends]
local_base_url = "http://localhost:7979/v1"
cloud_base_url = "https://openrouter.ai/api/v1"
local_models = []                                  # set per-deployment via TOML or LOCAL_MODELS env
local_context_limit = 60000
cloud_default_model = "anthropic/claude-sonnet-4.6"

[tiers.auto]
routing = "auto"
cloud_target = "z-ai/glm-5.2"
vision = "shim"
advertised_context = 1048576

[tiers.fast]
routing = "cloud"
cloud_target = "z-ai/glm-4.7-flash"
vision = "reject"
advertised_context = 202752

[tiers.balanced]
routing = "cloud"
cloud_target = "z-ai/glm-5.2"
vision = "shim"
advertised_context = 1048576

[tiers.reason]
routing = "cloud"
cloud_target = "moonshotai/kimi-k2.6"
vision = "native"
advertised_context = 262144

[tiers.deep]
routing = "cloud"
cloud_target = "google/gemini-2.5-pro"
vision = "native"
advertised_context = 1048576

[tiers.local]
routing = "local"
vision = "local"
# advertised_context omitted on purpose — the loader defaults it to local_context_limit
```

> **Verified against the live registry (`llm_router.py:144–189`):** these are the
> exact current values — note `balanced` vision = `shim` (not reject) and `reason`
> vision = `native` (not reject). `advertised_context` is a real per-tier field
> surfaced as `context_length` in `/v1/models` (`llm_router.py:1033`); fast=202752,
> reason=262144, the rest 1048576, and `local` tracks `local_context_limit`. `local`
> has no `cloud_target` (hard-fail tier).

- [ ] **Step 2: Write failing config tests in `test_config.py`**

```python
import textwrap
import loxo_llm_router.config as C


def _write(tmp_path, body):
    p = tmp_path / "loxo.toml"
    p.write_text(textwrap.dedent(body))
    return p


def test_defaults_load_loxo_namespace_and_tiers(monkeypatch):
    monkeypatch.delenv("LOXO_CONFIG", raising=False)
    monkeypatch.chdir("/")  # no ./loxo.toml
    cfg = C.load_config()
    assert cfg.namespace == "loxo"
    assert set(cfg.tiers) >= {
        "loxo/auto", "loxo/fast", "loxo/balanced",
        "loxo/reason", "loxo/deep", "loxo/local",
    }
    assert cfg.tiers["loxo/auto"].routing == "auto"
    assert cfg.tiers["loxo/auto"].cloud_target == "z-ai/glm-5.2"
    assert cfg.tiers["loxo/balanced"].vision == "shim"     # not reject
    assert cfg.tiers["loxo/reason"].vision == "native"     # not reject
    assert cfg.tiers["loxo/fast"].advertised_context == 202752
    assert cfg.tiers["loxo/reason"].advertised_context == 262144
    assert cfg.tiers["loxo/local"].routing == "local"
    assert cfg.tiers["loxo/local"].cloud_target is None
    # local tier's context window defaults to local_context_limit (60000 default)
    assert cfg.tiers["loxo/local"].advertised_context == cfg.local_context_limit


def test_toml_file_overrides_defaults(tmp_path, monkeypatch):
    cfg_path = _write(tmp_path, """
        namespace = "acme"
        [backends]
        local_context_limit = 12345
        [tiers.auto]
        routing = "auto"
        cloud_target = "vendor/model-x"
        vision = "shim"
    """)
    monkeypatch.setenv("LOXO_CONFIG", str(cfg_path))
    cfg = C.load_config()
    assert cfg.namespace == "acme"
    assert cfg.local_context_limit == 12345
    assert cfg.tiers["acme/auto"].cloud_target == "vendor/model-x"


def test_env_overrides_toml(tmp_path, monkeypatch):
    cfg_path = _write(tmp_path, 'namespace = "acme"\n[server]\nport = 9090\n')
    monkeypatch.setenv("LOXO_CONFIG", str(cfg_path))
    monkeypatch.setenv("ROUTER_NS", "zeta")
    monkeypatch.setenv("PORT", "9191")
    cfg = C.load_config()
    assert cfg.namespace == "zeta"      # env beats file
    assert cfg.port == 9191             # bare PORT honored (PaaS convention)
    assert "zeta/auto" in cfg.tiers     # ids follow the namespace


def test_loxo_port_beats_bare_port(monkeypatch):
    # Namespaced override wins over a (possibly inherited) bare PORT.
    monkeypatch.delenv("LOXO_CONFIG", raising=False)
    monkeypatch.chdir("/")
    monkeypatch.setenv("PORT", "8080")        # e.g. platform-injected
    monkeypatch.setenv("LOXO_PORT", "9595")   # escape hatch
    cfg = C.load_config()
    assert cfg.port == 9595


def test_secrets_come_only_from_env(tmp_path, monkeypatch):
    # API key in the TOML must be ignored; env is the only source.
    cfg_path = _write(tmp_path, 'openrouter_api_key = "leaked-from-file"\n')
    monkeypatch.setenv("LOXO_CONFIG", str(cfg_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    cfg = C.load_config()
    assert cfg.openrouter_api_key == "sk-from-env"


def test_local_models_env_is_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("LOXO_CONFIG", str(_write(tmp_path, "namespace='loxo'\n")))
    monkeypatch.setenv("LOCAL_MODELS", "a-model, b-model ,")
    cfg = C.load_config()
    assert cfg.local_models == ("a-model", "b-model")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python3 -m pytest test_config.py -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (no `config` module / `load_config` yet).

- [ ] **Step 4: Implement `loxo_llm_router/config.py`**

```python
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class VirtualModel:
    id: str
    routing: str = "auto"            # "auto" | "cloud" | "local"
    cloud_target: str | None = None
    local_target: str | None = None
    vision: str = "shim"             # "shim" | "native" | "reject" | "local"
    advertised_context: int = 1_048_576   # surfaced as context_length in /v1/models


@dataclass(frozen=True)
class Config:
    namespace: str
    host: str
    port: int
    local_base_url: str
    cloud_base_url: str
    local_models: tuple[str, ...]
    local_context_limit: int
    cloud_default_model: str
    tiers: dict[str, VirtualModel]
    # secrets (env-only)
    openrouter_api_key: str
    router_token: str
    quiet: bool


def _bundled_defaults() -> dict:
    text = files("loxo_llm_router").joinpath("loxo.default.toml").read_text()
    return tomllib.loads(text)


def _user_config_path() -> Path | None:
    env = os.environ.get("LOXO_CONFIG")
    if env:
        return Path(env)
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    for cand in (Path.cwd() / "loxo.toml", xdg / "loxo-llm-router" / "loxo.toml"):
        if cand.is_file():
            return cand
    return None


def _merge(base: dict, overlay: dict) -> dict:
    """Two-level merge: top-level scalars replace; section dicts merge per-key."""
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merged = dict(base[k])
            merged.update(v)
            base[k] = merged
        else:
            base[k] = v
    return base


def load_config() -> Config:
    data = _bundled_defaults()
    user = _user_config_path()
    if user is not None:
        with open(user, "rb") as f:
            _merge(data, tomllib.load(f))

    ns = os.environ.get("ROUTER_NS") or data.get("namespace", "loxo")
    server = data.get("server", {})
    backends = data.get("backends", {})

    # Bind address: namespaced LOXO_* wins (escape hatch), then bare HOST/PORT
    # (12-factor / PaaS convention — Heroku, Cloud Run, etc. inject PORT), then TOML.
    host = (os.environ.get("LOXO_HOST") or os.environ.get("HOST")
            or server.get("host", "0.0.0.0"))
    port = int(os.environ.get("LOXO_PORT") or os.environ.get("PORT")
               or server.get("port", 9090))

    local_base_url = (os.environ.get("LOCAL_BASE_URL")
                      or backends.get("local_base_url", "http://localhost:7979/v1")).rstrip("/")
    cloud_base_url = (os.environ.get("CLOUD_BASE_URL")
                      or backends.get("cloud_base_url", "https://openrouter.ai/api/v1")).rstrip("/")

    lm_env = os.environ.get("LOCAL_MODELS")
    if lm_env is not None:
        local_models = tuple(m.strip() for m in lm_env.split(",") if m.strip())
    else:
        local_models = tuple(backends.get("local_models", []))

    local_context_limit = int(os.environ.get("LOCAL_CONTEXT_LIMIT")
                              or backends.get("local_context_limit", 60000))
    cloud_default_model = (os.environ.get("CLOUD_DEFAULT_MODEL")
                           or backends.get("cloud_default_model", "anthropic/claude-sonnet-4.6"))

    tiers: dict[str, VirtualModel] = {}
    for key, t in data.get("tiers", {}).items():
        tid = f"{ns}/{key}"
        routing = t.get("routing", "auto")
        # Per-tier context window; local tier defaults to the local context limit
        # so it tracks the backend, mirroring the original registry.
        adv = t.get("advertised_context")
        if adv is None:
            adv = local_context_limit if routing == "local" else 1_048_576
        tiers[tid] = VirtualModel(
            id=tid,
            routing=routing,
            cloud_target=t.get("cloud_target"),
            local_target=t.get("local_target"),
            vision=t.get("vision", "shim"),
            advertised_context=int(adv),
        )

    return Config(
        namespace=ns,
        host=host,
        port=port,
        local_base_url=local_base_url,
        cloud_base_url=cloud_base_url,
        local_models=local_models,
        local_context_limit=local_context_limit,
        cloud_default_model=cloud_default_model,
        tiers=tiers,
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        router_token=os.environ.get("ROUTER_TOKEN", ""),
        quiet=os.environ.get("ROUTER_QUIET", "") == "1",
    )
```

- [ ] **Step 5: Run config tests to verify they pass**

Run: `python3 -m pytest test_config.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add loxo_llm_router/config.py loxo_llm_router/loxo.default.toml test_config.py
git commit -m "feat: layered TOML config loader (defaults < loxo.toml < env), secrets env-only"
```

---

### Task 3: Wire the app to the config object + de-brand

Replace the scattered import-time `os.environ.get(...)` globals (and the inline `VIRTUAL_MODELS` construction) with one `load_config()` call; route `owned_by` and error-message remedies through the resolved namespace/ids; remove every `airwolf` reference.

**Files:**
- Modify: `loxo_llm_router/__init__.py` (config block ~118–200; `VIRTUAL_MODELS` ~145–190; `owned_by` ~1032; error messages ~637, ~646–647, ~652, ~763, ~771; module docstring ~30–90; remove the old `VirtualModel` dataclass definition)
- Modify: `test_routing.py` (any `airwolf/` literals → `loxo/`; `owned_by` assertion → `loxo-llm-router`)

**Interfaces:**
- Consumes: `load_config()`, `Config`, `VirtualModel` from `loxo_llm_router.config` (Task 2).
- Produces (module globals, names unchanged for downstream code): `LOCAL_BASE_URL`, `CLOUD_BASE_URL`, `OPENROUTER_API_KEY`, `LOCAL_MODELS_ORDER`, `LOCAL_MODELS`, `LOCAL_CONTEXT_LIMIT`, `CLOUD_DEFAULT_MODEL`, `QUIET`, `ROUTER_TOKEN`, `VIRTUAL_MODELS`, `ROUTER_NS`, `AUTO_ID`, `DEEP_ID`, `LOCAL_TIER_ID`; re-exported `VirtualModel`.

- [ ] **Step 1: Replace the config/registry block in `__init__.py`**

Remove the old `VirtualModel` dataclass definition and the entire scattered env-read block + inline `VIRTUAL_MODELS` construction, and replace with:

```python
from .config import Config, VirtualModel, load_config

_cfg: Config = load_config()

LOCAL_BASE_URL = _cfg.local_base_url
CLOUD_BASE_URL = _cfg.cloud_base_url
OPENROUTER_API_KEY = _cfg.openrouter_api_key
LOCAL_MODELS_ORDER = list(_cfg.local_models)
LOCAL_MODELS = set(LOCAL_MODELS_ORDER)
LOCAL_CONTEXT_LIMIT = _cfg.local_context_limit
CLOUD_DEFAULT_MODEL = _cfg.cloud_default_model
QUIET = _cfg.quiet
ROUTER_TOKEN = _cfg.router_token
VIRTUAL_MODELS = _cfg.tiers
ROUTER_NS = _cfg.namespace
HOST = _cfg.host
PORT = _cfg.port

AUTO_ID = f"{ROUTER_NS}/auto"
DEEP_ID = f"{ROUTER_NS}/deep"
LOCAL_TIER_ID = f"{ROUTER_NS}/local"
```

> Keep the `import os`, `httpx`, FastAPI, etc. imports. `VirtualModel` is now imported (re-exported), so `R.VirtualModel` still resolves in tests.

- [ ] **Step 2: Set `owned_by` and de-brand error messages**

- `owned_by` (~1032): `"owned_by": f"{ROUTER_NS}-llm-router",`
- Error messages (~637, ~646–647, ~652, ~763, ~771): replace literal `airwolf/auto`/`airwolf/deep` with f-strings using `AUTO_ID`/`DEEP_ID`/`LOCAL_TIER_ID`, e.g.:

```python
                f"this model tier has no vision; switch to {AUTO_ID} or {DEEP_ID}"
```
```python
                f"{LOCAL_TIER_ID} has no local OCR configured (VISION_SHIM_MODEL unset); "
                f"switch to {DEEP_ID} for vision"
```
```python
                f"image isn't text-readable locally; switch to {DEEP_ID} for native vision"
```
```python
                        f"switch to {AUTO_ID} or {DEEP_ID}"),
```
```python
                        f"or switch to {AUTO_ID}"),
```

- [ ] **Step 3: Update the module docstring**

In the top docstring, delete the per-tier env-var documentation block (`AUTO_MODEL_ID`, `FAST_*`, `DEEP_*`, `BALANCED_*`, `REASON_*`, `LOCAL_TIER_*`) — those vars no longer exist — and replace with a short pointer:

```
  Configuration: see loxo.default.toml for the full schema. Precedence is
  bundled defaults < ./loxo.toml (or $LOXO_CONFIG / ~/.config/loxo-llm-router/
  loxo.toml) < environment. Tiers are defined under [tiers.*]; the tier id is
  "<namespace>/<table-key>". Secrets (OPENROUTER_API_KEY, ROUTER_TOKEN) are
  read ONLY from the environment.

  Env overrides: ROUTER_NS, HOST, PORT, LOCAL_BASE_URL, CLOUD_BASE_URL,
  LOCAL_MODELS, LOCAL_CONTEXT_LIMIT, CLOUD_DEFAULT_MODEL, LOG_CONFIG,
  ROUTER_QUIET, LOXO_CONFIG.
```

Also fix the `LOCAL_BASE_URL` example host (`mac-mini.tailnet-name.ts.net` → `localhost`).

- [ ] **Step 4: Replace `airwolf` literals in `test_routing.py`**

Replace every remaining `airwolf/` with `loxo/` and `airwolf-llm-router` with `loxo-llm-router`. Fix the header docstring command (any hardcoded interpreter path → `python3 -m pytest test_routing.py -q`).

> Tests that monkeypatch `R.VIRTUAL_MODELS` or construct `R.VirtualModel(id="loxo/...")` keep working — only the string literals change. The registry-default assertions now expect `loxo/*` ids and `owned_by == "loxo-llm-router"`.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all PASS (routing + config).

- [ ] **Step 6: Confirm no `airwolf` and no retired env vars remain in code**

Run:
```bash
grep -rin airwolf loxo_llm_router test_routing.py test_config.py
grep -rnE "AUTO_MODEL_ID|FAST_MODEL_ID|DEEP_MODEL_ID|BALANCED_MODEL_ID|REASON_MODEL_ID|LOCAL_TIER_MODEL_ID|FAST_CLOUD_MODEL|DEEP_CLOUD_MODEL" loxo_llm_router
```
Expected: no output from either.

- [ ] **Step 7: Commit**

```bash
git add loxo_llm_router/__init__.py test_routing.py
git commit -m "refactor: drive app from layered config; rebrand to loxo; retire per-tier env vars"
```

---

### Task 4: Cross-platform launch — console entry + portable serve script

**Files:**
- Modify: `loxo_llm_router/__init__.py` (append `main()` + `__main__` guard)
- Modify: `llm-router-serve.sh` (full rewrite)

**Interfaces:**
- Consumes: module globals `HOST`, `PORT` (Task 3).
- Produces: `main() -> None` — console entry; serves `loxo_llm_router:app` on `HOST`/`PORT`; optional `LOG_CONFIG`. Consumed by `pyproject.toml` `[project.scripts]` in Task 5.

- [ ] **Step 1: Add `main()`**

Append to `loxo_llm_router/__init__.py`:

```python
def main() -> None:
    """Console entry point (loxo-llm-router): serve the app under uvicorn.

    Host/port come from the resolved config (defaults < loxo.toml < env).
    LOG_CONFIG (optional path) selects a uvicorn log-config JSON; otherwise
    logging goes to stdout/stderr.
    """
    import uvicorn

    log_config = os.environ.get("LOG_CONFIG") or None
    uvicorn.run("loxo_llm_router:app", host=HOST, port=PORT, log_config=log_config)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify import + entry callable**

Run: `python3 -c "import loxo_llm_router as m; assert callable(m.main); print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Rewrite `llm-router-serve.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Repo dir derived from this script's own location — no hardcoded paths.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Optional env file (secrets/overrides; chmod 600). Override path with LOXO_ENV_FILE.
ENV_FILE="${LOXO_ENV_FILE:-$HOME/.config/loxo-llm-router/env}"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

export TZ="${TZ:-UTC}"
PYTHON="${PYTHON:-python3}"

# UTC-timestamped uvicorn formatters ship in the repo; use if present.
LOG_CONFIG="${LOG_CONFIG:-$SCRIPT_DIR/llm-router-logconfig.json}"
LOG_ARGS=()
[ -f "$LOG_CONFIG" ] && LOG_ARGS=(--log-config "$LOG_CONFIG")

# Host/port resolve from config; allow shell overrides too.
exec "$PYTHON" -m uvicorn \
  --app-dir "$SCRIPT_DIR" \
  "${LOG_ARGS[@]}" \
  --host "${LOXO_HOST:-${HOST:-0.0.0.0}}" \
  --port "${LOXO_PORT:-${PORT:-9090}}" \
  loxo_llm_router:app
```

- [ ] **Step 4: Verify the script serves, then stop it**

Run:
```bash
chmod +x llm-router-serve.sh
PORT=9099 ./llm-router-serve.sh &
SVPID=$!; sleep 3
curl -fs http://127.0.0.1:9099/health && echo " <- health ok"
kill "$SVPID"
```
Expected: health line then `<- health ok`. (Install deps first if needed: `pip install fastapi "uvicorn[standard]" httpx`.)

- [ ] **Step 5: Confirm no personal paths in the script**

Run: `grep -nE "/Users/brendanl|\.venvs|/opt/homebrew|Library/Logs" llm-router-serve.sh`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add loxo_llm_router/__init__.py llm-router-serve.sh
git commit -m "feat: portable launch — main() console entry + path-independent serve script"
```

---

### Task 5: Packaging — `pyproject.toml` (incl. package data) + `LICENSE`

**Files:**
- Create: `pyproject.toml`
- Create: `LICENSE`

**Interfaces:**
- Consumes: `loxo_llm_router:main`, `loxo_llm_router:app`, bundled `loxo_llm_router/loxo.default.toml`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "loxo-llm-router"
version = "0.1.0"
description = "OpenAI-compatible router that dispatches each request to a local or cloud LLM backend by intent — not retry-on-failure."
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
authors = [{ name = "Brendan LeFebvre" }]
keywords = ["llm", "openai", "proxy", "router", "openrouter", "mlx", "ollama", "local-llm"]
classifiers = [
  "License :: OSI Approved :: MIT License",
  "Programming Language :: Python :: 3 :: Only",
  "Programming Language :: Python :: 3.11",
  "Topic :: Scientific/Engineering :: Artificial Intelligence",
]
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "httpx>=0.27",
]

[project.urls]
Homepage = "https://github.com/BrendanL79/loxo-llm-router"
Repository = "https://github.com/BrendanL79/loxo-llm-router"

[project.scripts]
loxo-llm-router = "loxo_llm_router:main"

[tool.setuptools]
packages = ["loxo_llm_router"]

[tool.setuptools.package-data]
loxo_llm_router = ["loxo.default.toml"]

[tool.pytest.ini_options]
addopts = "-q"
```

> No `requirements.txt` — `pyproject.toml` is the single source of truth (DRY).

- [ ] **Step 2: Create `LICENSE` (MIT)**

```
MIT License

Copyright (c) 2026 Brendan LeFebvre

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Clean-venv install; verify console script + bundled TOML are present**

Run:
```bash
python3 -m venv /tmp/loxo-venv
/tmp/loxo-venv/bin/pip install -q .
/tmp/loxo-venv/bin/python -c "import loxo_llm_router; from importlib.resources import files; print('toml:', files('loxo_llm_router').joinpath('loxo.default.toml').is_file())"
ls /tmp/loxo-venv/bin/loxo-llm-router && echo "script ok"
```
Expected: `toml: True` and `script ok`. (`toml: True` proves package-data shipped — the zero-config defaults survive install.)

- [ ] **Step 4: Verify the installed console script serves from a non-repo dir**

Run:
```bash
( cd /tmp && PORT=9098 /tmp/loxo-venv/bin/loxo-llm-router & echo $! > /tmp/loxo.pid )
sleep 3
curl -fs http://127.0.0.1:9098/v1/models | python3 -c "import sys,json; ids=[m['id'] for m in json.load(sys.stdin)['data']]; assert 'loxo/auto' in ids, ids; print('served ok:', ids)"
kill "$(cat /tmp/loxo.pid)"; rm -rf /tmp/loxo-venv /tmp/loxo.pid
```
Expected: `served ok: [... 'loxo/auto' ...]`. (Running from `/tmp` proves it does not depend on the repo working dir.)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml LICENSE
git commit -m "build: pyproject packaging (py>=3.11), bundled default config, MIT license"
```

---

### Task 6: Containerization — `Dockerfile`, `.dockerignore`, `compose`

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`

- [ ] **Step 1: Create `.dockerignore`**

```
.git
.github
docs
vendor
.claude
.superpowers
.pytest_cache
__pycache__
*.pyc
.env
env
*.env
loxo.env
```

- [ ] **Step 2: Create `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY loxo_llm_router ./loxo_llm_router
RUN pip install --no-cache-dir .

EXPOSE 9090
ENV HOST=0.0.0.0 \
    PORT=9090 \
    TZ=UTC

CMD ["loxo-llm-router"]
```

- [ ] **Step 3: Create `docker-compose.yml`**

```yaml
services:
  loxo-llm-router:
    build: .
    image: loxo-llm-router:latest
    ports:
      - "9090:9090"
    env_file:
      - ./loxo.env            # copy .env.example -> loxo.env and fill in secrets
    volumes:
      - ./loxo.toml:/app/loxo.toml:ro   # optional: mount your tier catalog
    extra_hosts:
      - "host.docker.internal:host-gateway"   # reach a model server on the host (Linux)
    restart: unless-stopped
```

> The `loxo.toml` volume is optional; without it the container uses bundled defaults + env. Document that if no `loxo.toml` exists, remove the volume line or `touch loxo.toml`.

- [ ] **Step 4: Build the image**

Run: `docker build -t loxo-llm-router:test .`
Expected: build completes successfully. (If Docker is unavailable here, defer Steps 4–5 to a Docker-capable machine and note it; do not mark the task complete without a successful build somewhere.)

- [ ] **Step 5: Run the container and hit /health, then stop it**

Run:
```bash
docker run -d --name loxo-test -p 9097:9090 -e OPENROUTER_API_KEY=test loxo-llm-router:test
sleep 4
curl -fs http://127.0.0.1:9097/health && echo " <- docker ok"
docker rm -f loxo-test
```
Expected: health line then `<- docker ok`.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml
git commit -m "build: Dockerfile, compose, dockerignore"
```

---

### Task 7: Documentation for adopters

**Files:**
- Create: `loxo.toml.example`
- Create: `.env.example`
- Modify: `README.md` (full rewrite)
- Modify: `AGENTS.md` (genericize)
- Create: `docs/deploy.md`

- [ ] **Step 1: Create `loxo.toml.example`**

Copy `loxo_llm_router/loxo.default.toml` and add a sensible `local_models` example + comments:

```toml
# loxo-llm-router configuration.
# Precedence: bundled defaults < this file < environment variables.
# Secrets (OPENROUTER_API_KEY, ROUTER_TOKEN) are read ONLY from the environment.
# Search order: $LOXO_CONFIG -> ./loxo.toml -> ~/.config/loxo-llm-router/loxo.toml

namespace = "loxo"            # tier ids become loxo/auto, loxo/fast, ... ; ROUTER_NS env overrides

[server]
host = "0.0.0.0"
port = 9090

[backends]
local_base_url = "http://localhost:7979/v1"   # MLX / Ollama / llama.cpp / vLLM / LM Studio
cloud_base_url = "https://openrouter.ai/api/v1"
local_models = ["mlx-community/Qwen3.6-35B-A3B-4bit"]   # first entry = default local target
local_context_limit = 60000
cloud_default_model = "anthropic/claude-sonnet-4.6"

# Each [tiers.<key>] becomes the virtual id "<namespace>/<key>".
# advertised_context is surfaced as context_length in /v1/models.
[tiers.auto]
routing = "auto"              # local-first; escalate on size / x-quality: best
cloud_target = "z-ai/glm-5.2"
vision = "shim"
advertised_context = 1048576

[tiers.fast]
routing = "cloud"
cloud_target = "z-ai/glm-4.7-flash"
vision = "reject"
advertised_context = 202752

[tiers.balanced]
routing = "cloud"
cloud_target = "z-ai/glm-5.2"
vision = "shim"
advertised_context = 1048576

[tiers.reason]
routing = "cloud"
cloud_target = "moonshotai/kimi-k2.6"
vision = "native"
advertised_context = 262144

[tiers.deep]
routing = "cloud"
cloud_target = "google/gemini-2.5-pro"
vision = "native"
advertised_context = 1048576

[tiers.local]
routing = "local"            # pinned local, hard-fail (no cloud fallback)
vision = "local"
# advertised_context omitted → defaults to local_context_limit
```

- [ ] **Step 2: Create `.env.example` (secrets + optional overrides only)**

```bash
# loxo-llm-router — environment. Secrets live here, NOT in loxo.toml.
# serve-script default location: ~/.config/loxo-llm-router/env (chmod 600)
# docker-compose: copy to ./loxo.env

# --- Secrets (env-only) ---
OPENROUTER_API_KEY=sk-or-...
# ROUTER_TOKEN=                 # optional shared secret; clients must send matching bearer

# --- Optional deploy overrides (otherwise from loxo.toml / defaults) ---
# LOXO_CONFIG=/path/to/loxo.toml
# ROUTER_NS=loxo
# LOXO_HOST=0.0.0.0       # namespaced override (wins over bare HOST)
# LOXO_PORT=9090          # namespaced override (wins over bare PORT)
# HOST=0.0.0.0            # bare names also honored (12-factor / PaaS convention)
# PORT=9090               # e.g. Heroku / Cloud Run inject PORT automatically
# LOCAL_BASE_URL=http://localhost:7979/v1
# CLOUD_BASE_URL=https://openrouter.ai/api/v1
# LOCAL_MODELS=mlx-community/Qwen3.6-35B-A3B-4bit
# LOCAL_CONTEXT_LIMIT=60000
# CLOUD_DEFAULT_MODEL=anthropic/claude-sonnet-4.6
# LOG_CONFIG=/path/to/llm-router-logconfig.json
# ROUTER_QUIET=0
```

- [ ] **Step 3: Rewrite `README.md`**

```markdown
# loxo-llm-router

An OpenAI-compatible router that dispatches each request to a **local** (MLX,
Ollama, llama.cpp, vLLM, LM Studio — anything OpenAI-compatible) or **cloud**
(OpenRouter) backend based on **intent-expressing heuristics**, not
retry-on-failure. Clients send one virtual model id; the router resolves it to
the right backend and model.

```
clients ──▶ loxo-llm-router (:9090/v1) ──┬──▶ local model server (:7979/v1)
                                         └──▶ OpenRouter (cloud)
```

## Install & run

```bash
pip install .                 # Python >= 3.11
export OPENROUTER_API_KEY=sk-or-...
loxo-llm-router               # serves on 0.0.0.0:9090 with bundled defaults
```

Docker:

```bash
cp .env.example loxo.env      # add your OPENROUTER_API_KEY
docker compose up --build
```

## Configure

Configuration layers, lowest to highest precedence:

1. **bundled defaults** — runs out of the box
2. **`loxo.toml`** — your tier catalog + app settings (copy `loxo.toml.example`)
3. **environment** — overrides scalars, and is the **only** place for secrets

Config-file search order: `$LOXO_CONFIG` → `./loxo.toml` →
`~/.config/loxo-llm-router/loxo.toml`. See `loxo.toml.example` for the full
schema and `.env.example` for environment options.

**Secrets** (`OPENROUTER_API_KEY`, `ROUTER_TOKEN`) are read only from the
environment — never put them in `loxo.toml`.

**Host/port** resolve `LOXO_HOST`/`LOXO_PORT` → bare `HOST`/`PORT` → `[server]`
in `loxo.toml` → default `0.0.0.0:9090`. The bare `PORT` is honored so platforms
that inject it (Heroku, Cloud Run, Railway) work with no extra config; set
`LOXO_PORT` to force a value regardless of any platform-set `PORT`. Docker is the
default deployment — a container's environment is hermetic, so the bare names are
safe there.

## Tiers

Tiers are defined in `[tiers.*]`; each becomes the virtual id
`<namespace>/<key>`. Defaults:

| Tier | Routing | Vision |
|---|---|---|
| `loxo/auto` | local-first; escalate on size / `x-quality: best` | shim |
| `loxo/fast` | pinned cloud | reject images (422) |
| `loxo/balanced` | pinned cloud | reject |
| `loxo/reason` | pinned cloud | reject |
| `loxo/deep` | pinned cloud | native |
| `loxo/local` | pinned local, hard-fail (no cloud fallback) | local OCR only |

Rebrand the whole namespace by setting `namespace` in `loxo.toml` (or
`ROUTER_NS`). Add/retarget tiers by editing the `[tiers.*]` table.

## Routing rules (first match wins)

1. `model` matches a tier id → resolve by that tier's policy
2. `model` matches a `local_models` entry → local
3. `x-quality: best` header → cloud
4. estimated prompt > `local_context_limit` → cloud
5. `model` contains `/` (provider-prefixed) → cloud
6. default → local

Plus a fallback: if local is chosen but unreachable (or disconnects
mid-stream), forward to cloud with `cloud_default_model` — except for the
hard-fail `loxo/local` tier.

## Deploying

See [docs/deploy.md](docs/deploy.md) for Docker, systemd (Linux), and launchd
(macOS) recipes.

## License

MIT — see [LICENSE](LICENSE).
```

- [ ] **Step 4: Genericize `AGENTS.md`**

- Replace the hardcoded interpreter line with: *"Run with any Python ≥3.11 that has the deps installed (`pip install .`)."*
- Replace `~/bin` symlink + `com.local.llm-router` LaunchAgent + `launchctl kill` paragraphs with: *"Deployment recipes (Docker / systemd / launchd) live in `docs/deploy.md`."*
- Replace the `~/Library/Logs/...` lines with: *"Logs go to stdout/stderr; the process manager decides where they land. UTC formatting via `LOG_CONFIG=llm-router-logconfig.json`."*
- Update the "Config / Virtual models" sections to describe the **TOML config layer** (defaults < `loxo.toml` < env; secrets env-only; tiers in `[tiers.*]`); remove references to the retired per-tier env vars; change `airwolf/` → `loxo/`.

- [ ] **Step 5: Create `docs/deploy.md`**

```markdown
# Deploying loxo-llm-router

Serves on `:9090`, logs to stdout/stderr. Config: `loxo.toml` (see
`../loxo.toml.example`) + env for secrets (`../.env.example`).

## Docker (recommended, all platforms)

```bash
cp .env.example loxo.env          # fill in OPENROUTER_API_KEY
cp loxo.toml.example loxo.toml    # optional: edit your tier catalog
docker compose up --build -d
curl http://localhost:9090/health
```

If your model server runs on the host, set
`local_base_url = "http://host.docker.internal:7979/v1"` in `loxo.toml`.

## systemd (Linux, user service)

`~/.config/systemd/user/loxo-llm-router.service`:

```ini
[Unit]
Description=loxo-llm-router
After=network-online.target

[Service]
EnvironmentFile=%h/.config/loxo-llm-router/env
ExecStart=%h/.local/bin/loxo-llm-router
Environment=TZ=UTC
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now loxo-llm-router
journalctl --user -u loxo-llm-router -f
```

## launchd (macOS)

`~/Library/LaunchAgents/com.loxo.llm-router.plist` (find the binary with
`which loxo-llm-router`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.loxo.llm-router</string>
  <key>ProgramArguments</key>
  <array><string>/usr/local/bin/loxo-llm-router</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>TZ</key><string>UTC</string></dict>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/loxo-llm-router.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/loxo-llm-router.err.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.loxo.llm-router.plist
```
```

- [ ] **Step 6: Verify docs carry no personal paths or old brand**

Run:
```bash
grep -rinE "airwolf|/Users/brendanl|mac-mini|\.venvs|Library/Logs/llm-router" \
  README.md AGENTS.md .env.example loxo.toml.example docs/deploy.md
```
Expected: no output. (The launchd `/tmp/...` and `/usr/local/bin` paths are intentional and fine.)

- [ ] **Step 7: Commit**

```bash
git add README.md AGENTS.md .env.example loxo.toml.example docs/deploy.md
git commit -m "docs: TOML-config README, examples, deploy recipes; genericize AGENTS.md"
```

---

### Task 8: Repo hygiene — strip internal artifacts

**Files:**
- Modify: `.gitignore`
- Remove (working tree): `vendor/`
- Untrack (if tracked): `.claude/`, `.superpowers/`, `.pytest_cache/`
- Create: `docs/superpowers/README.md`

- [ ] **Step 1: Extend `.gitignore`**

Append:

```gitignore
# Packaging / build
*.egg-info/
build/
dist/

# Tooling / editor / agent state (not for public snapshot)
.claude/
.superpowers/
.pytest_cache/
vendor/

# Local config / container env
loxo.toml
loxo.env
```

> `loxo.toml` is gitignored on purpose — users create their own from `loxo.toml.example`. The shipped defaults live in `loxo_llm_router/loxo.default.toml`, which is NOT ignored.

- [ ] **Step 2: Remove the untracked vendored fork**

Run: `rm -rf vendor`
Then `git status --short` — expected: no `?? vendor/`.

- [ ] **Step 3: Untrack tooling dirs**

Run: `git rm -r --cached --ignore-unmatch .claude .superpowers .pytest_cache`

- [ ] **Step 4: Mark internal docs as snapshot-excluded**

Create `docs/superpowers/README.md`:

```markdown
# Internal — exclude from public snapshot

Design specs and implementation plans used during development. Do not copy
`docs/superpowers/` into the public `loxo-llm-router` snapshot. Also exclude
`.claude/`, `.superpowers/`, and `.pytest_cache/`.
```

- [ ] **Step 5: Commit**

```bash
git add .gitignore docs/superpowers/README.md
git commit -m "chore: gitignore build/tooling/local-config; untrack agent state; mark internal docs"
```

---

### Task 9: Final release verification

**Files:** none (verification only).

- [ ] **Step 1: Full test suite passes**

Run: `python3 -m pytest -q`
Expected: all green (routing + config), zero failures.

- [ ] **Step 2: No old brand in shipping files**

Run:
```bash
grep -rin airwolf . --include='*.py' --include='*.sh' --include='*.toml' \
  --include='*.md' --include='*.json' --include='*.yml' --include='*.yaml' \
  | grep -v 'docs/superpowers/'
```
Expected: no output.

- [ ] **Step 3: No personal paths or retired env vars in shipping files**

Run:
```bash
grep -rinE "/Users/brendanl|mac-mini|\.venvs|tailnet|Library/Logs/llm-router" . \
  --include='*.py' --include='*.sh' --include='*.toml' --include='*.md' \
  --include='*.json' --include='*.yml' --include='*.yaml' | grep -v 'docs/superpowers/'
grep -rnE "FAST_MODEL_ID|DEEP_CLOUD_MODEL|BALANCED_MODEL_ID|REASON_CLOUD_MODEL|LOCAL_TIER_MODEL_ID" \
  loxo_llm_router README.md AGENTS.md loxo.toml.example .env.example
```
Expected: no output from either.

- [ ] **Step 4: Fresh-venv install + zero-config smoke (run from /tmp)**

Run:
```bash
python3 -m venv /tmp/loxo-rel && /tmp/loxo-rel/bin/pip install -q .
( cd /tmp && PORT=9096 /tmp/loxo-rel/bin/loxo-llm-router & echo $! > /tmp/loxo-rel.pid )
sleep 3
curl -fs http://127.0.0.1:9096/v1/models | python3 -c "import sys,json; d=json.load(sys.stdin); ids=[m['id'] for m in d['data']]; assert 'loxo/auto' in ids, ids; assert all(m['owned_by']=='loxo-llm-router' for m in d['data']); print('release ok:', ids)"
kill "$(cat /tmp/loxo-rel.pid)"; rm -rf /tmp/loxo-rel /tmp/loxo-rel.pid
```
Expected: `release ok: [... 'loxo/auto' ...]` — proves install, bundled defaults, branding, and repo-independence all work together.

- [ ] **Step 5: Docker image builds (if Docker available)**

Run: `docker build -t loxo-llm-router:rel . && echo "docker build ok"`
Expected: `docker build ok`. (Defer to a Docker-capable machine if unavailable; note the deferral.)

- [ ] **Step 6: Clean tree on `public-release`**

Run: `git status --short --branch`
Expected: `## public-release`, no stray untracked files (no `vendor/`).

---

## Snapshot handoff (manual, performed by Brendan)

The public snapshot is the working tree **minus** internal dirs:

```bash
rsync -a --exclude='.git' --exclude='docs/superpowers' --exclude='.claude' \
      --exclude='.superpowers' --exclude='.pytest_cache' --exclude='vendor' \
      ./ ../loxo-llm-router-public/
cd ../loxo-llm-router-public && git init && git add -A \
  && git commit -m "Initial public release of loxo-llm-router"
```

Then create `BrendanL79/loxo-llm-router` and push.

---

## Self-Review

- **Spec coverage:** package promotion (T1) ✓; layered TOML config with defaults<toml<env and secrets env-only (T2) ✓; app wired to config + rebrand + retired per-tier env vars (T3) ✓; Python ≥3.11 / stdlib tomllib (T2 import, T5 floor) ✓; cross-platform launch + logs (T4) ✓; packaging incl. bundled config as package-data (T5) ✓; Docker (T6) ✓; README/examples/deploy/AGENTS (T7) ✓; hygiene incl. vendor/, .claude/, gitignored loxo.toml (T8) ✓; verification (T9) ✓. Original asks — alternate name `loxo` and Mac-specific items like log location — covered by T3 and T4/T7. The three deliberate decisions (hybrid config, 3.11 floor, package structure) are all reflected.
- **Placeholder scan:** every code/config step has full content; verification steps give exact commands + expected output. Registry values resolved against the live `llm_router.py:144–189` (balanced vision=shim, reason vision=native, per-tier `advertised_context` carried through). Remaining flagged conditional: `~line numbers` in `__init__.py` (grep to confirm), and Docker "defer if unavailable."
- **Type consistency:** `VirtualModel` defined once in `config.py` (T2), re-exported by `__init__.py` (T3) so `R.VirtualModel` resolves in tests; `Config`/`load_config()` signatures used identically across T2/T3/T4; `loxo_llm_router:main` and `loxo_llm_router:app` referenced consistently in `main()` (T4), `pyproject.toml` (T5), serve script (T4), and Dockerfile (T6); module globals produced in T3 (`HOST`, `PORT`, `VIRTUAL_MODELS`, …) are exactly those consumed by `main()` and the existing routing code.
```