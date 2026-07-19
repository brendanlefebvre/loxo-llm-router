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
    reasoning: str | None = None     # "low" | "medium" | "high" -> OpenRouter reasoning.effort (B2)


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
    """Three-level merge: top scalars replace; section dicts merge per-key;
    tiers merge per-field so a partial [tiers.X] only overrides the fields named."""
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            if k == "tiers":
                # Per-tier field merge: only named fields are overridden
                for tier_key, tier_v in v.items():
                    if isinstance(tier_v, dict) and isinstance(base[k].get(tier_key), dict):
                        base[k][tier_key] = {**base[k][tier_key], **tier_v}
                    else:
                        base[k][tier_key] = tier_v
            else:
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
        reasoning = t.get("reasoning")
        if reasoning is not None and reasoning not in ("low", "medium", "high"):
            raise ValueError(f"tiers.{key}: reasoning must be low|medium|high, got {reasoning!r}")
        tiers[tid] = VirtualModel(
            id=tid,
            routing=routing,
            cloud_target=t.get("cloud_target"),
            local_target=t.get("local_target"),
            vision=t.get("vision", "shim"),
            advertised_context=int(adv),
            reasoning=reasoning,
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
