"""Derived local-context threshold: explicit config > startup probe of
{LOCAL_BASE_URL}/models > HF cache of the served model's config.json > legacy
60000 default. A down local server must never block routing (probe failure ->
cache stays None -> fallback)."""
import asyncio
import json

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
    monkeypatch.setattr(R, "_hf_derived_context", None)  # cache tier: nothing found either
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


# --- HF cache tier: _hf_cache_context(repo) ----------------------------------

def _write_hf_config(tmp_path, monkeypatch, repo, config, revision="abc123"):
    """Build a fake HF hub cache tree under tmp_path and point HF_HOME at it:
    {tmp_path}/hub/models--{org}--{name}/snapshots/{revision}/config.json"""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    model_dir = "models--" + repo.replace("/", "--")
    snap_dir = tmp_path / "hub" / model_dir / "snapshots" / revision
    snap_dir.mkdir(parents=True)
    (snap_dir / "config.json").write_text(json.dumps(config))


def test_hf_cache_top_level_max_position_embeddings(tmp_path, monkeypatch):
    repo = "mlx-community/Qwen3-14B-4bit"
    _write_hf_config(tmp_path, monkeypatch, repo, {"max_position_embeddings": 40960})
    assert R._hf_cache_context(repo) == 40960


def test_hf_cache_nested_text_config_max_position_embeddings(tmp_path, monkeypatch):
    repo = "mlx-community/Qwen3.6-VL-30B"
    _write_hf_config(tmp_path, monkeypatch, repo,
                      {"text_config": {"max_position_embeddings": 262144}})
    assert R._hf_cache_context(repo) == 262144


def test_hf_cache_top_level_beats_nested(tmp_path, monkeypatch):
    repo = "mlx-community/Some-Model"
    _write_hf_config(tmp_path, monkeypatch, repo, {
        "max_position_embeddings": 8192,
        "text_config": {"max_position_embeddings": 262144},
    })
    assert R._hf_cache_context(repo) == 8192


def test_hf_cache_missing_model_dir_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert R._hf_cache_context("mlx-community/Not-Cached") is None


def test_hf_cache_malformed_json_is_none(tmp_path, monkeypatch):
    repo = "mlx-community/Broken-Config"
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    model_dir = "models--" + repo.replace("/", "--")
    snap_dir = tmp_path / "hub" / model_dir / "snapshots" / "rev1"
    snap_dir.mkdir(parents=True)
    (snap_dir / "config.json").write_text("{not valid json")
    assert R._hf_cache_context(repo) is None


def test_hf_cache_bool_does_not_resolve_as_int(tmp_path, monkeypatch):
    repo = "mlx-community/Weird-Config"
    _write_hf_config(tmp_path, monkeypatch, repo, {"max_position_embeddings": True})
    assert R._hf_cache_context(repo) is None


def test_hf_cache_empty_repo_is_none(monkeypatch):
    assert R._hf_cache_context(None) is None
    assert R._hf_cache_context("") is None


# --- HF cache tier: which model, and the once-per-process cache -------------

def test_cached_hf_local_context_empty_local_models_is_none(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", [])
    monkeypatch.setattr(R, "_hf_derived_context", R._HF_CONTEXT_UNSET)
    assert R._cached_hf_local_context() is None


def test_cached_hf_local_context_computed_once(tmp_path, monkeypatch):
    calls = []

    def fake_hf_cache_context(repo):
        calls.append(repo)
        return 12345

    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Qwen3-14B-4bit"])
    monkeypatch.setattr(R, "_hf_derived_context", R._HF_CONTEXT_UNSET)
    monkeypatch.setattr(R, "_hf_cache_context", fake_hf_cache_context)

    assert R._cached_hf_local_context() == 12345
    assert R._cached_hf_local_context() == 12345
    assert len(calls) == 1  # hot path: not re-statting on the second call


def test_effective_local_context_hot_path_not_restatting(monkeypatch):
    """effective_local_context() must not re-derive the HF-cache tier on
    every call once it has been computed once."""
    calls = []

    def fake_hf_cache_context(repo):
        calls.append(repo)
        return 12345

    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", None)
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Qwen3-14B-4bit"])
    monkeypatch.setattr(R, "_hf_derived_context", R._HF_CONTEXT_UNSET)
    monkeypatch.setattr(R, "_hf_cache_context", fake_hf_cache_context)

    assert R.effective_local_context() == 12345
    assert R.effective_local_context() == 12345
    assert len(calls) == 1


# --- precedence: explicit > probe > HF cache > legacy default ---------------

def test_precedence_explicit_beats_hf_cache(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 999)
    monkeypatch.setattr(R, "_derived_local_context", None)
    monkeypatch.setattr(R, "_hf_derived_context", 40960)
    assert R.effective_local_context() == 999


def test_precedence_probe_beats_hf_cache(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", 8888)
    monkeypatch.setattr(R, "_hf_derived_context", 40960)
    assert R.effective_local_context() == 8888


def test_precedence_hf_cache_beats_legacy_default(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", None)
    monkeypatch.setattr(R, "_hf_derived_context", 40960)
    assert R.effective_local_context() == 40960
