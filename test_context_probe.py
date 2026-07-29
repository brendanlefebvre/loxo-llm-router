"""Derived local-context threshold: explicit config > startup probe of
{LOCAL_BASE_URL}/models > legacy 60000 default. A down local server must
never block routing (probe failure -> cache stays None -> fallback)."""
import asyncio

from fastapi.testclient import TestClient

import loxo_llm_router as R


def test_payload_parser_context_length():
    payload = {"data": [{"id": "m", "context_length": 262144}]}
    assert R._context_from_models_payload(payload) == 262144


def test_payload_parser_max_model_len():
    payload = {"data": [{"id": "m", "max_model_len": 40960}]}
    assert R._context_from_models_payload(payload) == 40960


def test_payload_parser_min_across_entries():
    # Conservative: a server hosting several models gates at the smallest.
    payload = {"data": [{"context_length": 262144},
                        {"context_length": 40960}]}
    assert R._context_from_models_payload(payload) == 40960


def test_payload_parser_absent_or_garbage_is_none():
    assert R._context_from_models_payload({"data": [{"id": "m"}]}) is None
    assert R._context_from_models_payload({}) is None
    assert R._context_from_models_payload(
        {"data": [{"context_length": "big"}]}) is None


def test_effective_explicit_config_wins(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 12345)
    monkeypatch.setattr(R, "_derived_local_context", 262144)
    assert R.effective_local_context() == 12345


def test_effective_probe_when_no_explicit(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", 262144)
    assert R.effective_local_context() == 262144


def test_effective_legacy_default_last(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", None)
    assert R.effective_local_context() == 60000


# --- probe_local_context: the async network call itself ---------------------

class _Success:
    """Stub httpx.AsyncClient whose GET returns a well-formed /models payload."""
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, *a, **k): return _SuccessResponse()


class _SuccessResponse:
    def json(self):
        return {"data": [{"context_length": 40960}]}


class _Boom:
    """Stub AsyncClient whose GET raises a transport-level failure."""
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, *a, **k): raise R.httpx.ConnectError("boom")


class _GarbageResponse:
    """A response whose body isn't valid JSON."""
    def json(self):
        raise ValueError("garbage")


class _Garbage:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, *a, **k): return _GarbageResponse()


def test_probe_success_fills_cache(monkeypatch):
    monkeypatch.setattr(R, "_derived_local_context", None)
    monkeypatch.setattr(R.httpx, "AsyncClient", _Success)
    asyncio.run(R.probe_local_context())
    assert R._derived_local_context == 40960


def test_probe_unreachable_server_caches_none(monkeypatch):
    # Pre-seed a stale value to prove failure actively resets the cache,
    # rather than merely leaving an already-None cache alone.
    monkeypatch.setattr(R, "_derived_local_context", 123)
    monkeypatch.setattr(R.httpx, "AsyncClient", _Boom)
    asyncio.run(R.probe_local_context())
    assert R._derived_local_context is None


def test_probe_garbage_response_caches_none(monkeypatch):
    monkeypatch.setattr(R, "_derived_local_context", 123)
    monkeypatch.setattr(R.httpx, "AsyncClient", _Garbage)
    asyncio.run(R.probe_local_context())
    assert R._derived_local_context is None


# --- startup hook -------------------------------------------------------------

def test_startup_hook_runs_the_probe(monkeypatch):
    monkeypatch.setattr(R, "_derived_local_context", None)
    monkeypatch.setattr(R.httpx, "AsyncClient", _Success)
    # starlette's TestClient only fires "startup" handlers while used as a
    # context manager; monkeypatching must land before entry so the suite
    # never risks a real network call.
    with TestClient(R.app):
        pass
    assert R._derived_local_context == 40960
