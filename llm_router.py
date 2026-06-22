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
  LOCAL_BASE_URL         e.g. http://mac-mini.tailnet-name.ts.net:7979/v1
  CLOUD_BASE_URL         default https://openrouter.ai/api/v1
  OPENROUTER_API_KEY     required for cloud traffic
  LOCAL_MODELS           comma-separated, e.g. "qwen3.6-35b-a3b,qwen3-30b-a3b"
  LOCAL_CONTEXT_LIMIT    default 60000 (tokens; ~240k chars)
  CLOUD_DEFAULT_MODEL    default anthropic/claude-sonnet-4.6
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
  SPEND_LEDGER           path to an append-only JSONL file that persists cloud
                         spend across restarts (default
                         ~/.config/llm-router/spend.jsonl). Set to "" to
                         disable durability (in-memory only). The file is
                         read once on startup to seed the accumulator.

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
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import StreamingResponse

LOCAL_BASE_URL = os.environ.get("LOCAL_BASE_URL", "http://localhost:7979/v1").rstrip("/")
CLOUD_BASE_URL = os.environ.get("CLOUD_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
LOCAL_MODELS = {
    m.strip() for m in os.environ.get("LOCAL_MODELS", "").split(",") if m.strip()
}


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


LOCAL_CONTEXT_LIMIT = int(os.environ.get("LOCAL_CONTEXT_LIMIT", "60000"))
CLOUD_DEFAULT_MODEL = os.environ.get("CLOUD_DEFAULT_MODEL", "anthropic/claude-sonnet-4.6")
LOCAL_CONNECT_TIMEOUT = float(os.environ.get("LOCAL_CONNECT_TIMEOUT", "5"))
QUIET = os.environ.get("ROUTER_QUIET", "") == "1"
# Optional shared-secret gate. If set, clients must send `Authorization: Bearer <token>`
# (e.g. set OpenCode's apiKey to this value). Empty = no auth (current behavior).
# The router proxies to PAID cloud with your key, so on a multi-client LAN this
# stops anyone who can reach the port from spending your OpenRouter credits.
ROUTER_TOKEN = os.environ.get("ROUTER_TOKEN", "")

_DEFAULT_LEDGER = pathlib.Path.home() / ".config" / "llm-router" / "spend.jsonl"
_ledger_env = os.environ.get("SPEND_LEDGER", str(_DEFAULT_LEDGER))
SPEND_LEDGER = pathlib.Path(_ledger_env) if _ledger_env else None

# --- Spend accumulator ----------------------------------------------------------
# In-memory totals, seeded from SPEND_LEDGER on startup if configured.
# Guarded by _spend_lock; never let a ledger write failure break a response.
# Structure: by_provider[hostname][model] -> {usd, requests}
_spend_lock = asyncio.Lock()
_spend_total_usd: float = 0.0
_spend_requests: int = 0
_spend_since: str = datetime.now(timezone.utc).isoformat()
_spend_by_provider: dict[str, dict[str, Any]] = {}  # host -> {total_usd, requests, by_model}


def _provider_host(base_url: str) -> str:
    """Extract the hostname from a base URL to use as the provider key."""
    from urllib.parse import urlparse
    return urlparse(base_url).hostname or base_url


def _accumulate(provider: str, model: str, usd: float) -> None:
    """Update in-memory totals (must be called with _spend_lock held)."""
    global _spend_total_usd, _spend_requests
    _spend_total_usd += usd
    _spend_requests += 1
    if provider not in _spend_by_provider:
        _spend_by_provider[provider] = {"total_usd": 0.0, "requests": 0, "by_model": {}}
    p = _spend_by_provider[provider]
    p["total_usd"] += usd
    p["requests"] += 1
    if model not in p["by_model"]:
        p["by_model"][model] = {"usd": 0.0, "requests": 0}
    p["by_model"][model]["usd"] += usd
    p["by_model"][model]["requests"] += 1


def _seed_spend_from_ledger() -> None:
    """Read SPEND_LEDGER at startup and populate the in-memory accumulator."""
    global _spend_since
    if not SPEND_LEDGER or not SPEND_LEDGER.exists():
        return
    earliest: str | None = None
    try:
        for raw in SPEND_LEDGER.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            usd = float(entry.get("usd", 0) or 0)
            if usd <= 0:
                continue
            model = entry.get("model", "unknown")
            provider = entry.get("provider", "unknown")
            ts = entry.get("ts", "")
            _accumulate(provider, model, usd)
            if ts and (earliest is None or ts < earliest):
                earliest = ts
        if earliest:
            _spend_since = earliest
    except Exception as e:
        print(f"[router] spend ledger seed failed ({e}); starting fresh")


_seed_spend_from_ledger()


async def record_cost(provider: str, model: str, usd: float, stream: bool, reason: str) -> None:
    """Thread-safe: update in-memory totals and append to the JSONL ledger."""
    if usd <= 0:
        return
    async with _spend_lock:
        _accumulate(provider, model, usd)
        total = _spend_total_usd

    log(f"[router] cloud cost=${usd:.6f} provider={provider} model={model} total=${total:.6f}")

    if SPEND_LEDGER:
        entry = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "usd": usd,
            "stream": stream,
            "reason": reason,
        })
        try:
            SPEND_LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with SPEND_LEDGER.open("a") as f:
                f.write(entry + "\n")
        except Exception as e:
            log(f"[router] spend ledger write failed ({e}); cost still counted in memory")


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
)

app = FastAPI()


def log(msg: str) -> None:
    if not QUIET:
        print(msg)


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


async def apply_vision_policy(
    body: dict[str, Any],
    base_url: str,
    model_to_send: str,
    reason: str,
    x_vision: str | None,
) -> tuple[str, str, dict[str, Any], str]:
    """Decide how to handle image content when the target model is text-only.

    Modes (the `x-vision` request header overrides the VISION_MODE default):
      local - OCR each image locally (mlx_vlm), feed the text to the text model
      cloud - reroute the whole request to VISION_CLOUD_MODEL (a multimodal cloud
              model) with the images left intact, so it sees and answers
      auto  - OCR locally; if the transcription is thin (< VISION_OCR_MIN_CHARS,
              i.e. probably a non-text image) escalate to cloud vision, else use
              the local OCR text

    Returns (base_url, model_to_send, body, reason) - possibly rerouted to cloud.
    No-op when the target already sees, there are no images, or nothing's configured.
    """
    if is_vision_capable(model_to_send):
        return base_url, model_to_send, body, reason
    if not count_images(body):
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

    # Explicit cloud, or auto/local with no local OCR model configured -> full reroute.
    if mode == "cloud" or not VISION_SHIM_MODEL:
        if VISION_CLOUD_MODEL:
            return _reroute_to_cloud("cloud")
        log("[router] vision=cloud requested but VISION_CLOUD_MODEL unset; image passes through")
        return base_url, model_to_send, body, reason

    # local or auto, with a local OCR model available -> OCR first.
    shimmed, n_chars = await apply_vision_shim(body)

    if mode == "local":
        log(f"[router] vision=local: OCR'd image(s) -> {n_chars} chars for {model_to_send}")
        return base_url, model_to_send, shimmed, f"{reason}+vision-local"

    # auto: rich OCR -> keep local text; thin OCR -> escalate to cloud (if configured).
    if n_chars >= VISION_OCR_MIN_CHARS or not VISION_CLOUD_MODEL:
        log(f"[router] vision=auto: OCR {n_chars} chars -> local text for {model_to_send}")
        return base_url, model_to_send, shimmed, f"{reason}+vision-auto-local"
    log(f"[router] vision=auto: OCR thin ({n_chars} < {VISION_OCR_MIN_CHARS}) -> escalating to cloud")
    return _reroute_to_cloud("auto-escalated")


def pick_target(body: dict[str, Any], quality_header: str | None) -> tuple[str, str, str]:
    """Return (base_url, model_to_send, reason) for logging."""
    model = body.get("model", "")

    if is_local_model(model):
        return LOCAL_BASE_URL, model, "explicit-local-model"

    if (quality_header or "").lower() == "best":
        return CLOUD_BASE_URL, model or CLOUD_DEFAULT_MODEL, "quality-best"

    if estimate_prompt_tokens(body) > LOCAL_CONTEXT_LIMIT:
        return CLOUD_BASE_URL, model or CLOUD_DEFAULT_MODEL, "prompt-too-long"

    if "/" in model:
        return CLOUD_BASE_URL, model, "provider-prefixed"

    return LOCAL_BASE_URL, model, "default-local"


def _headers_for(url: str, client_headers: dict[str, str]) -> dict[str, str]:
    h = {
        k: v for k, v in client_headers.items()
        if k.lower() not in {"host", "authorization", "content-length", "accept-encoding"}
    }
    if url == CLOUD_BASE_URL and OPENROUTER_API_KEY:
        h["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"
    return h


def _extract_cost_from_sse_line(line: bytes) -> float | None:
    """Parse a single SSE `data: {...}` line; return usage.cost if present."""
    try:
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("data:"):
            return None
        payload = text[5:].strip()
        if payload == "[DONE]":
            return None
        obj = json.loads(payload)
        cost = obj.get("usage", {}) or {}
        val = cost.get("cost")
        return float(val) if val is not None else None
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
):
    """Forward to primary_url; on a transport failure, transparently retry against
    fallback_url (if given). For streaming, the fallback only applies before the
    first byte has been yielded — a partially-sent stream cannot be restarted.

    cloud_model / cloud_provider: when set, the served response came from cloud
    and we should extract usage.cost and call record_cost.
    """

    if stream:
        async def streamer():
            client = httpx.AsyncClient(timeout=TIMEOUT)
            served_cloud_model = cloud_model
            served_cloud_provider = cloud_provider
            served_reason = reason
            try:
                url, body = primary_url, primary_body
                can_fallback = fallback_url is not None
                while True:
                    yielded = False
                    try:
                        async with client.stream(
                            "POST", f"{url}{path}", content=body,
                            headers=_headers_for(url, client_headers),
                        ) as resp:
                            buf = b""
                            cost_found: float | None = None
                            async for chunk in resp.aiter_raw():
                                yielded = True
                                yield chunk
                                # Tee: scan for the terminal SSE usage chunk.
                                if served_cloud_model:
                                    buf += chunk
                                    # Process complete lines; keep partial tail.
                                    while b"\n" in buf:
                                        line, buf = buf.split(b"\n", 1)
                                        c = _extract_cost_from_sse_line(line)
                                        if c is not None:
                                            cost_found = c
                            if served_cloud_model and served_cloud_provider and cost_found is not None:
                                asyncio.ensure_future(
                                    record_cost(served_cloud_provider, served_cloud_model,
                                                cost_found, stream=True, reason=served_reason)
                                )
                        return
                    except FALLBACK_ERRORS:
                        if can_fallback and not yielded:
                            log(f"[router] stream: {url} unreachable pre-first-byte, "
                                f"falling back to cloud/{CLOUD_DEFAULT_MODEL}")
                            url, body = fallback_url, fallback_body  # type: ignore[assignment]
                            served_cloud_model = CLOUD_DEFAULT_MODEL
                            served_cloud_provider = _provider_host(CLOUD_BASE_URL)
                            served_reason = "fallback"
                            can_fallback = False
                            continue
                        raise
            finally:
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
            log(f"[router] {primary_url} unreachable, falling back to cloud/{CLOUD_DEFAULT_MODEL}")
            resp = await client.post(
                f"{fallback_url}{path}", content=fallback_body,
                headers=_headers_for(fallback_url, client_headers),
            )
            served_cloud_model = CLOUD_DEFAULT_MODEL
            served_cloud_provider = _provider_host(CLOUD_BASE_URL)
            served_reason = "fallback"

        if served_cloud_model and served_cloud_provider and resp.status_code == 200:
            try:
                cost = (json.loads(resp.content).get("usage") or {}).get("cost")
                if cost is not None:
                    await record_cost(served_cloud_provider, served_cloud_model,
                                      float(cost), stream=False, reason=served_reason)
            except Exception:
                pass

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
    stream = bool(body.get("stream", False))

    base_url, model_to_send, reason = pick_target(body, x_quality)
    body["model"] = model_to_send

    # Vision policy: when an image hits a text-only target model, handle it per
    # the local/cloud/auto policy (may reroute to a multimodal cloud model).
    # No-op unless configured; see apply_vision_policy.
    base_url, model_to_send, body, reason = await apply_vision_policy(
        body, base_url, model_to_send, reason, x_vision
    )

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

    # Only LOCAL gets a cloud fallback (cloud has no further fallback target).
    fallback_url: str | None = None
    fallback_body: bytes | None = None
    if base_url == LOCAL_BASE_URL:
        fb = dict(body)
        fb["model"] = CLOUD_DEFAULT_MODEL
        if stream:
            fb["stream_options"] = {**fb.get("stream_options", {}), "include_usage": True}
        fallback_url = CLOUD_BASE_URL
        fallback_body = json.dumps(fb).encode()

    # Pass cloud_model/provider so forward() can attribute usage.cost correctly.
    is_cloud = base_url == CLOUD_BASE_URL
    served_cloud_model = model_to_send if is_cloud else None
    served_cloud_provider = _provider_host(CLOUD_BASE_URL) if is_cloud else None

    return await forward(
        base_url, "/chat/completions", primary_body, dict(request.headers), stream,
        fallback_url=fallback_url, fallback_body=fallback_body,
        cloud_model=served_cloud_model, cloud_provider=served_cloud_provider, reason=reason,
    )


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    denied = auth_failed(authorization)
    if denied is not None:
        return denied
    merged: list[dict[str, Any]] = []
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
    async with _spend_lock:
        return {
            "total_usd": round(_spend_total_usd, 8),
            "requests": _spend_requests,
            "since": _spend_since,
            "ledger": str(SPEND_LEDGER) if SPEND_LEDGER else None,
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


@app.get("/health")
async def health():
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
        "spend": spend_summary,
    }
