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

  4. If the `model` field contains a "/" (e.g. "anthropic/claude-sonnet-4.5"),
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
  CLOUD_DEFAULT_MODEL    default anthropic/claude-sonnet-4.5
  LOCAL_CONNECT_TIMEOUT  default 5 (seconds; how fast "local is down" fails over)
  ROUTER_QUIET           set to "1" to silence per-request routing logs

Run:
  pip install fastapi uvicorn httpx
  uvicorn llm_router:app --host 0.0.0.0 --port 9090

Clients point any OpenAI-compatible SDK at http://<router-host>:9090/v1 .
"""

from __future__ import annotations

import json
import os
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
LOCAL_CONTEXT_LIMIT = int(os.environ.get("LOCAL_CONTEXT_LIMIT", "60000"))
CLOUD_DEFAULT_MODEL = os.environ.get("CLOUD_DEFAULT_MODEL", "anthropic/claude-sonnet-4.5")
LOCAL_CONNECT_TIMEOUT = float(os.environ.get("LOCAL_CONNECT_TIMEOUT", "5"))
QUIET = os.environ.get("ROUTER_QUIET", "") == "1"

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


async def forward(
    primary_url: str,
    path: str,
    primary_body: bytes,
    client_headers: dict[str, str],
    stream: bool,
    fallback_url: str | None = None,
    fallback_body: bytes | None = None,
):
    """Forward to primary_url; on a transport failure, transparently retry against
    fallback_url (if given). For streaming, the fallback only applies before the
    first byte has been yielded — a partially-sent stream cannot be restarted."""

    if stream:
        async def streamer():
            client = httpx.AsyncClient(timeout=TIMEOUT)
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
                            async for chunk in resp.aiter_raw():
                                yielded = True
                                yield chunk
                        return
                    except FALLBACK_ERRORS:
                        if can_fallback and not yielded:
                            log(f"[router] stream: {url} unreachable pre-first-byte, "
                                f"falling back to cloud/{CLOUD_DEFAULT_MODEL}")
                            url, body = fallback_url, fallback_body  # type: ignore[assignment]
                            can_fallback = False
                            continue
                        raise
            finally:
                await client.aclose()
        return StreamingResponse(streamer(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
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
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, x_quality: str | None = Header(default=None)):
    body_bytes = await request.body()
    body = json.loads(body_bytes)
    stream = bool(body.get("stream", False))

    base_url, model_to_send, reason = pick_target(body, x_quality)
    body["model"] = model_to_send
    primary_body = json.dumps(body).encode()

    log(f"[router] -> {base_url} model={model_to_send} reason={reason}")

    # Only LOCAL gets a cloud fallback (cloud has no further fallback target).
    fallback_url: str | None = None
    fallback_body: bytes | None = None
    if base_url == LOCAL_BASE_URL:
        fb = dict(body)
        fb["model"] = CLOUD_DEFAULT_MODEL
        fallback_url = CLOUD_BASE_URL
        fallback_body = json.dumps(fb).encode()

    return await forward(
        base_url, "/chat/completions", primary_body, dict(request.headers), stream,
        fallback_url=fallback_url, fallback_body=fallback_body,
    )


@app.get("/v1/models")
async def models():
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


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "local": LOCAL_BASE_URL,
        "cloud": CLOUD_BASE_URL,
        "local_models": sorted(LOCAL_MODELS),
        "local_context_limit": LOCAL_CONTEXT_LIMIT,
        "local_connect_timeout": LOCAL_CONNECT_TIMEOUT,
    }
