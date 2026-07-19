"""
LLM router: OpenAI-compatible proxy that dispatches each request to either
a local (MLX) endpoint or a cloud endpoint (default OpenRouter) based on
*intent-expressing* heuristics, not retry-on-failure.

Heuristics, in priority order (first match wins):

  1. If the request's `model` field matches a known LOCAL_MODELS entry,
     route to LOCAL.  (Explicit local intent.)

  2. If the client sends `x-quality: best` header, route to CLOUD.
     (Explicit quality intent.)

  3. If the estimated prompt size exceeds LOCAL_CONTEXT_LIMIT tokens,
     route to CLOUD.  (Local KV cache won't fit, regardless of the model's
     theoretical context window.)  Tool/function schemas are counted too.

  4. If the `model` field contains a "/" (e.g. "anthropic/claude-sonnet-4.6"),
     route to CLOUD.  (Provider-prefixed IDs are OpenRouter's convention.)

  5. Default: LOCAL.

A hard-error fallback is layered on top: if LOCAL is picked and the connection
is refused OR times out, we forward to CLOUD using CLOUD_DEFAULT_MODEL instead.
This is NOT a quality-signal fallback — it's only for "local is physically down
or wedged."  It works for streaming requests too, but only before the first
byte is sent (a partial stream can't be transparently restarted).

Configuration (env vars):
  LOCAL_BASE_URL         e.g. http://localhost:7979/v1
  CLOUD_BASE_URL         default https://openrouter.ai/api/v1
  OPENROUTER_API_KEY     required for cloud traffic
  LOCAL_MODELS           comma-separated, e.g. "qwen3.6-35b-a3b,qwen3-30b-a3b"
  LOCAL_CONTEXT_LIMIT    default 60000 (tokens; ~240k chars)
  CLOUD_DEFAULT_MODEL    default anthropic/claude-sonnet-4.6

  Configuration: see loxo.default.toml for the full schema. Precedence is
  bundled defaults < ./loxo.toml (or $LOXO_CONFIG / ~/.config/loxo-llm-router/
  loxo.toml) < environment. Tiers are defined under [tiers.*]; the tier id is
  "<namespace>/<table-key>". Secrets (OPENROUTER_API_KEY, ROUTER_TOKEN) are
  read ONLY from the environment.

  Env overrides: ROUTER_NS, HOST, PORT, LOCAL_BASE_URL, CLOUD_BASE_URL,
  LOCAL_MODELS, LOCAL_CONTEXT_LIMIT, CLOUD_DEFAULT_MODEL, LOG_CONFIG,
  ROUTER_QUIET, LOXO_CONFIG.

  RATE_CARD_TTL          seconds before the live rate card is refreshed
                         (default 86400). Fetch is non-blocking and best-effort.
  RATE_CARD_URL          pricing source (default OpenRouter /api/v1/models).
  LOCAL_CONNECT_TIMEOUT  default 5 (seconds; how fast "local is down" fails over)
  ROUTER_QUIET           set to "1" to silence per-request routing logs
  ROUTER_TOKEN           optional shared secret; if set, clients must send
                         `Authorization: Bearer <token>` or get 401. Guards the
                         money-spending cloud path on a multi-client LAN.
  VISION_SHIM_MODEL      opt-in: a local VLM model id. When set, image content
                         sent to a text-only target model is transcribed to text
                         via the VLM first, so e.g. GLM-5.2 can "read" a pasted
                         screenshot. Unset = images pass through untouched.
  VISION_SHIM_URL        VLM endpoint for the transcription (default LOCAL_BASE_URL)
  VISION_CAPABLE_MODELS  comma-separated tags of models that can already see;
                         the policy is skipped when the target matches one.
  VISION_MODE            default vision policy for image+text-only-model requests:
                         "auto" (OCR locally, escalate to cloud if OCR is thin),
                         "local" (OCR only), or "cloud" (always reroute to cloud).
                         Overridden per-request by the `x-vision` header.
  VISION_CLOUD_MODEL     multimodal cloud model to reroute image requests to in
                         cloud/auto-escalation modes (e.g. anthropic/claude-...).
  VISION_OCR_MIN_CHARS   auto-mode threshold; OCR shorter than this escalates.
  LOXO_STATE_DIR         root for operational state (ledgers). Default
                         $XDG_STATE_HOME/loxo-llm-router, i.e. usually
                         ~/.local/state/loxo-llm-router/. See ledger.py.
  SPEND_LEDGER           explicit path override for the spend ledger JSONL
                         (default $LOXO_STATE_DIR/spend.jsonl; a pre-existing
                         legacy ~/.config/loxo-llm-router/spend.jsonl keeps
                         working, with a logged pointer). Set to "" to disable
                         durability (in-memory only). Read once on startup to
                         seed the accumulator.
  ADEQUACY_LEDGER        path override for the A2 adequacy ledger JSONL
                         (default $LOXO_STATE_DIR/adequacy.jsonl; "" disables).
                         Metadata-only: class, route, outcome signals — never
                         message content. Observe-only in v0.2 (no routing
                         reads it).

Run:
  pip install fastapi uvicorn httpx
  uvicorn llm_router:app --host 0.0.0.0 --port 9090

Clients point any OpenAI-compatible SDK at http://<router-host>:9090/v1 .
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .classify import classify
from .config import Config, VirtualModel, load_config
from .ledger import (AdequacyLedger, Observation, SpendTracker, StreamScan,
                     resolve_adequacy_ledger, resolve_spend_ledger)


def _ts() -> str:
    """UTC ISO-8601 timestamp (second resolution, trailing Z) for log lines."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
LOCAL_CONNECT_TIMEOUT = float(os.environ.get("LOCAL_CONNECT_TIMEOUT", "5"))


def resolve_virtual(model_id: str) -> VirtualModel | None:
    """Return the VirtualModel for a client-facing id, or None for raw ids."""
    return VIRTUAL_MODELS.get(model_id)


def _provider_host(base_url: str) -> str:
    """Extract the hostname from a base URL to use as the provider key."""
    from urllib.parse import urlparse
    return urlparse(base_url).hostname or base_url


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


def _distinct_cloud_targets() -> set[str]:
    """Every non-falsy cloud_target in the registry (local-pinned tiers have none)."""
    return {vm.cloud_target for vm in VIRTUAL_MODELS.values() if vm.cloud_target}


async def get_rate_cards() -> dict[str, dict[str, Any]]:
    """Fetch + cache rate cards for every distinct cloud_target. Best-effort:
    on any failure, leaves the existing cache untouched and returns it."""
    global _rate_cards_fetched_at
    import time
    async with _rate_card_lock:
        targets = _distinct_cloud_targets()
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
                if vm.cloud_target == t and vm.vision == "shim" and card.get("input_modalities") == ["text"]:
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


# --- Vision shim --------------------------------------------------------------
# When a request carries image content but the chosen target model is text-only
# (e.g. GLM-5.2), transcribe each image to text via a local vision model
# (mlx_vlm) and substitute it in, so the text-only model still "sees" screenshots.
# OPT-IN: the shim is disabled unless VISION_SHIM_MODEL is set. When unset, the
# router behaves exactly as before (images pass through untouched).
#   VISION_SHIM_URL        VLM endpoint for transcription (default: LOCAL_BASE_URL)
#   VISION_SHIM_MODEL      VLM model id to request (e.g. an mlx_vlm vision model);
#                          empty = shim OFF
#   VISION_CAPABLE_MODELS  comma-separated tags of models that can already see;
#                          if the target matches one, the shim is skipped
#   VISION_SHIM_PROMPT     the transcription instruction sent to the VLM
VISION_SHIM_URL = os.environ.get("VISION_SHIM_URL", LOCAL_BASE_URL).rstrip("/")
VISION_SHIM_MODEL = os.environ.get("VISION_SHIM_MODEL", "")
VISION_CAPABLE_MODELS = {
    m.strip() for m in os.environ.get("VISION_CAPABLE_MODELS", "").split(",") if m.strip()
}
VISION_SHIM_PROMPT = os.environ.get(
    "VISION_SHIM_PROMPT",
    "Transcribe and describe this image in full detail for a text-only coding "
    "assistant. Include all visible text, code, error messages, file/UI labels, "
    "and overall layout. Output only the transcription, with no preamble.",
)

# Vision routing policy (applied when an image hits a text-only target model):
#   VISION_MODE          default policy: "auto" | "local" | "cloud".
#                        Per-request `x-vision` header overrides it.
#                          local = OCR the image locally, feed text to the text model
#                          cloud = reroute the whole request to a multimodal cloud model
#                          auto  = OCR locally; if the transcription is thin (likely a
#                                  non-text image), escalate to cloud vision
#   VISION_CLOUD_MODEL   multimodal cloud model to reroute to (e.g.
#                        "anthropic/claude-sonnet-4.6"); empty = cloud vision disabled
#   VISION_OCR_MIN_CHARS auto-mode threshold: OCR shorter than this escalates to cloud
VISION_MODE = os.environ.get("VISION_MODE", "auto").strip().lower()
VISION_CLOUD_MODEL = os.environ.get("VISION_CLOUD_MODEL", "")
VISION_OCR_MIN_CHARS = int(os.environ.get("VISION_OCR_MIN_CHARS", "40"))

# A long overall timeout (slow local generation is normal) but a SHORT connect
# timeout, so "local is down/wedged" fails over to cloud in seconds instead of
# hanging for the full read window.
TIMEOUT = httpx.Timeout(300.0, connect=LOCAL_CONNECT_TIMEOUT)

# Transport-level failures that should trigger the local->cloud fallback.
FALLBACK_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    # A local server that crashes/OOMs mid-prompt "disconnects without sending a
    # response" — i.e. wedged, not just down. The auto tier should degrade to
    # cloud rather than hard-fail. (Pre-first-byte only; a mid-stream disconnect
    # can't be transparently restarted.)
    httpx.RemoteProtocolError,
)

app = FastAPI()


def log(msg: str) -> None:
    if not QUIET:
        print(f"{_ts()} {msg}", flush=True)


# Spend tracking: totals seeded from the resolved ledger (see ledger.py for
# the LOXO_STATE_DIR / SPEND_LEDGER / legacy-path resolution rules).
SPEND = SpendTracker(resolve_spend_ledger(log), log=log)

# A2: adequacy ledger (observe-only) — the dial's only evidence source.
ADEQUACY = AdequacyLedger(resolve_adequacy_ledger(), log=log)


def auth_failed(authorization: str | None) -> Response | None:
    """If ROUTER_TOKEN is set, require a matching bearer token. Returns a 401
    Response on failure, or None to proceed. Constant-time compare."""
    if not ROUTER_TOKEN:
        return None
    provided = authorization or ""
    if provided[:7].lower() == "bearer ":
        provided = provided[7:]
    if not hmac.compare_digest(provided.strip(), ROUTER_TOKEN):
        return Response(
            content='{"error":{"message":"unauthorized: bad or missing router token",'
                    '"type":"invalid_request_error"}}',
            status_code=401,
            media_type="application/json",
        )
    return None


def estimate_prompt_tokens(body: dict[str, Any]) -> int:
    """~4 chars per token is a good English approximation; fine for threshold gating.

    Counts message content AND tool/function/system schemas — agentic clients
    (OpenCode) send large tool definitions that can dominate the real prompt size.
    """
    total = 0
    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(part.get("text", ""))
    for key in ("tools", "functions"):
        if key in body:
            total += len(json.dumps(body[key]))
    return total // 4


def is_local_model(model_name: str) -> bool:
    if not model_name or not LOCAL_MODELS:
        return False
    return any(tag in model_name for tag in LOCAL_MODELS)


def is_vision_capable(model_name: str) -> bool:
    """True if the model is known to handle image input (skip the shim for it)."""
    if not model_name or not VISION_CAPABLE_MODELS:
        return False
    return any(tag in model_name for tag in VISION_CAPABLE_MODELS)


def _iter_image_parts(body: dict[str, Any]):
    """Yield each OpenAI-format `image_url` content part in the request."""
    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    yield part


def count_images(body: dict[str, Any]) -> int:
    return sum(1 for _ in _iter_image_parts(body))


async def apply_vision_shim(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return (new_body, total_transcribed_chars). Every `image_url` part is
    replaced by a text transcription from the local VLM; the char count lets
    `auto` mode decide whether the OCR was rich enough or should escalate to
    cloud vision. Degrades gracefully: any image that fails to transcribe is
    left untouched, and the request is never broken.

    Note: routing (pick_target) runs BEFORE this, on the pre-transcription size,
    so a very large transcription could in theory overshoot a local context
    budget. Acceptable for v1; revisit if it bites.
    """
    transcribe_prompt = VISION_SHIM_PROMPT
    new_messages: list[dict[str, Any]] = []
    total_chars = 0
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for m in body.get("messages", []):
            content = m.get("content")
            if not isinstance(content, list):
                new_messages.append(m)
                continue
            new_content: list[dict[str, Any]] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    text = await _transcribe_image(client, url, transcribe_prompt) if url else None
                    if text:
                        total_chars += len(text)
                        new_content.append(
                            {"type": "text", "text": f"[Transcribed image:\n{text}\n]"}
                        )
                    else:
                        new_content.append(part)  # leave as-is on failure
                else:
                    new_content.append(part)
            nm = dict(m)
            nm["content"] = new_content
            new_messages.append(nm)
    nb = dict(body)
    nb["messages"] = new_messages
    return nb, total_chars


async def _transcribe_image(
    client: httpx.AsyncClient, image_url: str, prompt: str
) -> str | None:
    """Ask the local VLM to transcribe one image. Returns text, or None on any
    failure (so the caller leaves the original image part in place)."""
    payload = {
        "model": VISION_SHIM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        "stream": False,
        "temperature": 0,
    }
    try:
        r = await client.post(
            f"{VISION_SHIM_URL}/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code != 200:
            log(f"[router] vision-shim: VLM HTTP {r.status_code}; leaving image as-is")
            return None
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001 - never break the request over a shim failure
        log(f"[router] vision-shim: transcription failed ({e}); leaving image as-is")
        return None


class VisionRejected(Exception):
    """Image content hit a tier whose vision policy forbids serving it. The caller
    turns this into an HTTP 422 with .message so the user can switch tiers."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


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
                f"this model tier has no vision; switch to {AUTO_ID} or {DEEP_ID}"
            )
        return base_url, model_to_send, body, reason

    if vision_policy == "local":
        if not has_images:
            return base_url, model_to_send, body, reason
        if not VISION_SHIM_MODEL:
            raise VisionRejected(
                f"{LOCAL_TIER_ID} has no local OCR configured (VISION_SHIM_MODEL unset); "
                f"switch to {DEEP_ID} for vision"
            )
        shimmed, n_chars = await apply_vision_shim(body)
        if n_chars < VISION_OCR_MIN_CHARS:
            raise VisionRejected(
                f"image isn't text-readable locally; switch to {DEEP_ID} for native vision"
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


def pick_target(body: dict[str, Any], quality_header: str | None) -> tuple[str, str, str]:
    """Return (base_url, model_to_send, reason) for logging."""
    model = body.get("model", "")

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

    if is_local_model(model):
        return LOCAL_BASE_URL, model, "explicit-local-model"

    if (quality_header or "").lower() == "best":
        return CLOUD_BASE_URL, model or CLOUD_DEFAULT_MODEL, "quality-best"

    if estimate_prompt_tokens(body) > LOCAL_CONTEXT_LIMIT:
        return CLOUD_BASE_URL, model or CLOUD_DEFAULT_MODEL, "prompt-too-long"

    if "/" in model:
        return CLOUD_BASE_URL, model, "provider-prefixed"

    return LOCAL_BASE_URL, model, "default-local"


def cloud_fallback_for(base_url: str, vm: "VirtualModel | None") -> bool:
    """Whether a LOCAL-bound request may transparently fall back to cloud on a
    transport failure. Suppressed for the pinned-local tier so that an oversized
    prompt or a down local server hard-fails instead of silently spending cloud."""
    if base_url != LOCAL_BASE_URL:
        return False
    if vm is not None and vm.routing == "local":
        return False
    return True


async def _local_pin_preflight(
    body: dict[str, Any], vm: "VirtualModel | None"
) -> JSONResponse | None:
    """For the pinned-local tier, return a clear 422 if the request cannot be
    served locally — an oversized prompt or an unreachable local server — instead
    of letting it surface as a raw 500. Pinned-local never falls back to cloud, so
    these are hard-fails; the message names the remedy. Returns None when the
    request is fine (or the tier is not pinned-local)."""
    if not (vm and vm.routing == "local"):
        return None
    n = estimate_prompt_tokens(body)
    if n > LOCAL_CONTEXT_LIMIT:
        return JSONResponse(status_code=422, content={"error": {
            "message": (f"prompt is too large for local context "
                        f"(~{n} tokens > {LOCAL_CONTEXT_LIMIT}); "
                        f"switch to {AUTO_ID} or {DEEP_ID}"),
            "type": "invalid_request_error", "code": "local_context_exceeded"}})
    try:
        async with httpx.AsyncClient(timeout=LOCAL_CONNECT_TIMEOUT) as client:
            await client.get(f"{LOCAL_BASE_URL}/models")
    except FALLBACK_ERRORS:
        return JSONResponse(status_code=422, content={"error": {
            "message": (f"local server unreachable; start the MLX server "
                        f"or switch to {AUTO_ID}"),
            "type": "invalid_request_error", "code": "local_unreachable"}})
    return None


def _headers_for(url: str, client_headers: dict[str, str]) -> dict[str, str]:
    h = {
        k: v for k, v in client_headers.items()
        if k.lower() not in {"host", "authorization", "content-length", "accept-encoding"}
    }
    if url == CLOUD_BASE_URL and OPENROUTER_API_KEY:
        h["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"
    return h


def _extract_usage_from_sse_line(line: bytes) -> dict[str, Any] | None:
    """Parse a single SSE `data: {...}` line; return the usage object when the
    chunk carries a non-empty one (the terminal usage chunk), else None."""
    try:
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("data:"):
            return None
        payload = text[5:].strip()
        if payload == "[DONE]":
            return None
        usage = json.loads(payload).get("usage")
        return usage if isinstance(usage, dict) and usage else None
    except Exception:
        return None


async def forward(
    primary_url: str,
    path: str,
    primary_body: bytes,
    client_headers: dict[str, str],
    stream: bool,
    fallback_url: str | None = None,
    fallback_body: bytes | None = None,
    cloud_model: str | None = None,
    cloud_provider: str | None = None,
    reason: str = "",
    fallback_cloud_model: str | None = None,
    obs: "Observation | None" = None,
):
    """Forward to primary_url; on a transport failure, transparently retry against
    fallback_url (if given). For streaming, the fallback only applies before the
    first byte has been yielded — a partially-sent stream cannot be restarted.

    cloud_model / cloud_provider: when set, the served response came from cloud
    and we should extract usage.cost and call SPEND.record.

    obs: when set, filled with the outcome (A2, observe-only) and written to
    ADEQUACY once the response lands. Never affects routing or the bytes sent.
    """
    _t0 = asyncio.get_event_loop().time()

    if stream:
        client = httpx.AsyncClient(timeout=TIMEOUT)
        url, body = primary_url, primary_body
        served_cloud_model = cloud_model
        served_cloud_provider = cloud_provider
        served_reason = reason
        can_fallback = fallback_url is not None
        # Connect first (with pre-first-byte transport fallback), THEN peek the
        # upstream status before committing to a StreamingResponse. A generator
        # can only yield bytes — it can't set the HTTP status — so a non-200
        # (402 unfunded, 429, provider 5xx, a local error) must be caught here,
        # else it would surface to the client as a masked HTTP 200 carrying the
        # error body as if it were SSE. Mirrors the non-streaming path below.
        while True:
            try:
                resp = await client.send(
                    client.build_request(
                        "POST", f"{url}{path}", content=body,
                        headers=_headers_for(url, client_headers),
                    ),
                    stream=True,
                )
            except FALLBACK_ERRORS:
                if can_fallback:
                    fb_model = fallback_cloud_model or CLOUD_DEFAULT_MODEL
                    log(f"[router] stream: {url} unreachable pre-first-byte, "
                        f"falling back to cloud/{fb_model}")
                    url, body = fallback_url, fallback_body  # type: ignore[assignment]
                    served_cloud_model = fb_model
                    served_cloud_provider = _provider_host(CLOUD_BASE_URL)
                    served_reason = "fallback"
                    can_fallback = False
                    continue
                await client.aclose()
                raise
            break

        if obs is not None:
            obs.ttfb_ms = int((asyncio.get_event_loop().time() - _t0) * 1000) if _t0 else None
            obs.status = resp.status_code
            if served_reason == "fallback":
                obs.fallback_fired = True
                obs.route, obs.served_model = "cloud", served_cloud_model or ""
                obs.reason = "fallback"

        if resp.status_code != 200:
            err = await resp.aread()
            await resp.aclose()
            await client.aclose()
            try:
                content = json.loads(err)
            except Exception:
                text = err.decode("utf-8", "replace")
                if len(text) > 2000:
                    text = text[:2000] + "…(truncated)"
                content = {"error": {
                    "message": text or "upstream error",
                    "type": "upstream_error",
                }}
            log(f"[router] stream: upstream {url} returned {resp.status_code}; "
                f"surfacing status (no masked 200)")
            if obs is not None:
                obs.status = resp.status_code
                obs.latency_ms = int((asyncio.get_event_loop().time() - _t0) * 1000)
                asyncio.ensure_future(ADEQUACY.write(obs))
            return JSONResponse(status_code=resp.status_code, content=content)

        async def streamer():
            try:
                buf = b""
                scan = StreamScan()
                async for chunk in resp.aiter_raw():
                    yield chunk
                    # Tee: adequacy signals for every route; usage/cost for cloud.
                    buf += chunk
                    # Process complete lines; keep partial tail.
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        scan.feed_line(line)
                cost_found = (scan.usage or {}).get("cost")
                if served_cloud_model and served_cloud_provider and cost_found is not None:
                    asyncio.ensure_future(
                        SPEND.record(served_cloud_provider, served_cloud_model,
                                     float(cost_found), stream=True, reason=served_reason)
                    )
                if obs is not None:
                    obs.finish_reason = scan.finish_reason
                    obs.had_tool_calls = scan.had_tool_calls
                    obs.tool_calls_valid_json = scan.tool_calls_valid()
                    obs.usage = scan.usage
                    obs.usd = float(cost_found) if (served_cloud_model and cost_found) else 0.0
                    obs.latency_ms = int((asyncio.get_event_loop().time() - _t0) * 1000)
                    asyncio.ensure_future(ADEQUACY.write(obs))
            finally:
                await resp.aclose()
                await client.aclose()
        return StreamingResponse(streamer(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        served_cloud_model = cloud_model
        served_cloud_provider = cloud_provider
        served_reason = reason
        try:
            resp = await client.post(
                f"{primary_url}{path}", content=primary_body,
                headers=_headers_for(primary_url, client_headers),
            )
        except FALLBACK_ERRORS:
            if fallback_url is None:
                raise
            fb_model = fallback_cloud_model or CLOUD_DEFAULT_MODEL
            log(f"[router] {primary_url} unreachable, falling back to cloud/{fb_model}")
            resp = await client.post(
                f"{fallback_url}{path}", content=fallback_body,
                headers=_headers_for(fallback_url, client_headers),
            )
            served_cloud_model = fb_model
            served_cloud_provider = _provider_host(CLOUD_BASE_URL)
            served_reason = "fallback"

        cost_val = 0.0
        scan: StreamScan | None = None
        if resp.status_code == 200:
            try:
                scan = StreamScan.from_response_body(json.loads(resp.content))
            except Exception:
                scan = None
        if served_cloud_model and served_cloud_provider and scan is not None:
            cost = (scan.usage or {}).get("cost")
            if cost is not None:
                cost_val = float(cost)
                await SPEND.record(served_cloud_provider, served_cloud_model,
                                   cost_val, stream=False, reason=served_reason)

        if obs is not None:
            obs.status = resp.status_code
            if served_reason == "fallback":
                obs.fallback_fired = True
                obs.route, obs.served_model = "cloud", served_cloud_model or ""
                obs.reason = "fallback"
            if scan is not None:
                obs.finish_reason = scan.finish_reason
                obs.had_tool_calls = scan.had_tool_calls
                obs.tool_calls_valid_json = scan.tool_calls_valid()
                obs.usage = scan.usage
            obs.usd = cost_val
            obs.latency_ms = int((asyncio.get_event_loop().time() - _t0) * 1000)
            obs.ttfb_ms = obs.latency_ms  # non-streaming: single read
            asyncio.ensure_future(ADEQUACY.write(obs))

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    x_quality: str | None = Header(default=None),
    x_vision: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    denied = auth_failed(authorization)
    if denied is not None:
        return denied

    body_bytes = await request.body()
    body = json.loads(body_bytes)
    requested_model = body.get("model", "")
    stream = bool(body.get("stream", False))

    klass = classify(body)  # A1: observe-only, before any body rewrite

    requested_vm = resolve_virtual(body.get("model", ""))
    base_url, model_to_send, reason = pick_target(body, x_quality)
    body["model"] = model_to_send

    # Vision policy: when an image hits a text-only target model, handle it per
    # the local/cloud/auto policy (may reroute to a multimodal cloud model).
    # No-op unless configured; see apply_vision_policy.
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

    local_fail = await _local_pin_preflight(body, requested_vm)
    if local_fail is not None:
        return local_fail

    # stream_options.include_usage: inject on cloud-bound streaming bodies so
    # OpenCode (via @ai-sdk/openai-compatible) reads token counts from the final
    # SSE chunk and can display cost. Strip it from local-bound bodies — local
    # servers may not handle it and it serves no purpose there.
    if stream:
        if base_url == CLOUD_BASE_URL:
            body["stream_options"] = {**body.get("stream_options", {}), "include_usage": True}
        else:
            so = body.get("stream_options")
            if isinstance(so, dict) and "include_usage" in so:
                so = {k: v for k, v in so.items() if k != "include_usage"}
                if so:
                    body["stream_options"] = so
                else:
                    body.pop("stream_options")

    primary_body = json.dumps(body).encode()

    log(f"[router] -> {base_url} model={model_to_send} reason={reason}")

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

    # Pass cloud_model/provider so forward() can attribute usage.cost correctly.
    is_cloud = base_url == CLOUD_BASE_URL
    served_cloud_model = model_to_send if is_cloud else None
    served_cloud_provider = _provider_host(CLOUD_BASE_URL) if is_cloud else None

    # A2: one observation per request, filled by forward() as the outcome lands.
    obs = Observation(
        cls=klass.cls, classifier_version=klass.version,
        requested_model=requested_model,
        route="cloud" if is_cloud else "local",
        served_model=model_to_send, reason=reason, stream=stream,
    )

    return await forward(
        base_url, "/chat/completions", primary_body, dict(request.headers), stream,
        fallback_url=fallback_url, fallback_body=fallback_body,
        cloud_model=served_cloud_model, cloud_provider=served_cloud_provider, reason=reason,
        fallback_cloud_model=fallback_cloud_model, obs=obs,
    )


def _virtual_model_entries() -> list[dict[str, Any]]:
    """Synthesized /v1/models entries advertising the router's virtual models."""
    return [
        {
            "id": vm.id,
            "object": "model",
            "owned_by": f"{ROUTER_NS}-llm-router",
            "context_length": vm.advertised_context,
        }
        for vm in VIRTUAL_MODELS.values()
    ]


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    denied = auth_failed(authorization)
    if denied is not None:
        return denied
    merged: list[dict[str, Any]] = _virtual_model_entries()
    async with httpx.AsyncClient(timeout=30.0) as client:
        for base, auth in ((LOCAL_BASE_URL, None), (CLOUD_BASE_URL, OPENROUTER_API_KEY)):
            try:
                headers = {"Authorization": f"Bearer {auth}"} if auth else {}
                r = await client.get(f"{base}/models", headers=headers)
                if r.status_code == 200:
                    merged.extend(r.json().get("data", []))
            except Exception as e:
                log(f"[router] /models fetch from {base} failed: {e}")
    return {"object": "list", "data": merged}


@app.get("/v1/spend")
async def spend(authorization: str | None = Header(default=None)):
    """Return aggregated cloud spend tracked by this router instance.

    Totals are seeded from SPEND_LEDGER on startup (all-time) and updated
    live per request. `since` reflects the earliest ledger entry, or the
    process start time if the ledger is empty or disabled.
    """
    denied = auth_failed(authorization)
    if denied is not None:
        return denied
    cards = _rate_cards_snapshot_and_maybe_refresh()
    data = await SPEND.snapshot()
    return {
        "total_usd": data["total_usd"],
        "requests": data["requests"],
        "since": data["since"],
        "ledger": str(SPEND.ledger_path) if SPEND.ledger_path else None,
        "rate_cards": cards,
        "by_provider": data["by_provider"],
    }


@app.get("/health")
async def health():
    cards = _rate_cards_snapshot_and_maybe_refresh()
    data = await SPEND.snapshot()
    spend_summary = {k: data[k] for k in ("total_usd", "requests", "since")}
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
