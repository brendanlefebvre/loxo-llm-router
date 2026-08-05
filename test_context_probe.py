"""Derived local-context threshold: explicit config > startup probe of
{LOCAL_BASE_URL}/models > HF cache of the served model's config.json > legacy
60000 default. A down local server must never block routing (probe failure ->
cache stays None -> fallback)."""
import asyncio
import json
import threading
import time

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
    monkeypatch.setattr(R, "_hf_derived_config", None)  # cache tier: nothing found either
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


# --- HF cache tier: _hf_cache_config(repo) ----------------------------------

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
    assert R._config_context(R._hf_cache_config(repo)) == 40960


def test_hf_cache_nested_text_config_max_position_embeddings(tmp_path, monkeypatch):
    repo = "mlx-community/Qwen3.6-VL-30B"
    _write_hf_config(tmp_path, monkeypatch, repo,
                      {"text_config": {"max_position_embeddings": 262144}})
    assert R._config_context(R._hf_cache_config(repo)) == 262144


def test_hf_cache_top_level_beats_nested(tmp_path, monkeypatch):
    repo = "mlx-community/Some-Model"
    _write_hf_config(tmp_path, monkeypatch, repo, {
        "max_position_embeddings": 8192,
        "text_config": {"max_position_embeddings": 262144},
    })
    assert R._config_context(R._hf_cache_config(repo)) == 8192


def test_hf_cache_missing_model_dir_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert R._config_context(R._hf_cache_config("mlx-community/Not-Cached")) is None


def test_hf_cache_malformed_json_is_none(tmp_path, monkeypatch):
    repo = "mlx-community/Broken-Config"
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    model_dir = "models--" + repo.replace("/", "--")
    snap_dir = tmp_path / "hub" / model_dir / "snapshots" / "rev1"
    snap_dir.mkdir(parents=True)
    (snap_dir / "config.json").write_text("{not valid json")
    assert R._config_context(R._hf_cache_config(repo)) is None


def test_hf_cache_bool_does_not_resolve_as_int(tmp_path, monkeypatch):
    repo = "mlx-community/Weird-Config"
    _write_hf_config(tmp_path, monkeypatch, repo, {"max_position_embeddings": True})
    assert R._config_context(R._hf_cache_config(repo)) is None


def test_hf_cache_empty_repo_is_none(monkeypatch):
    assert R._config_context(R._hf_cache_config(None)) is None
    assert R._config_context(R._hf_cache_config("")) is None


# --- HF cache tier: which model, and the once-per-process cache -------------

def test_cached_hf_local_context_empty_local_models_is_none(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", [])
    monkeypatch.setattr(R, "_hf_derived_config", R._HF_CONFIG_UNSET)
    assert R._cached_hf_local_context() is None


def test_cached_hf_local_context_computed_once(tmp_path, monkeypatch):
    calls = []

    def fake_hf_cache_config(repo):
        calls.append(repo)
        return {"max_position_embeddings": 12345}

    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Qwen3-14B-4bit"])
    monkeypatch.setattr(R, "_hf_derived_config", R._HF_CONFIG_UNSET)
    monkeypatch.setattr(R, "_hf_cache_config", fake_hf_cache_config)

    assert R._cached_hf_local_context() == 12345
    assert R._cached_hf_local_context() == 12345
    assert len(calls) == 1  # hot path: not re-statting on the second call


def test_context_and_family_share_one_cache_read(monkeypatch):
    """Both facts come from the same config.json. Reading it twice would
    double the exposure to the hang HF_CACHE_READ_TIMEOUT exists to bound."""
    calls = []

    def fake_hf_cache_config(repo):
        calls.append(repo)
        return {"max_position_embeddings": 12345, "model_type": "qwen3"}

    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Qwen3-14B-4bit"])
    monkeypatch.setattr(R, "_hf_derived_config", R._HF_CONFIG_UNSET)
    monkeypatch.setattr(R, "_hf_cache_config", fake_hf_cache_config)

    assert R._cached_hf_local_context() == 12345
    assert R.local_model_family() == "qwen3"
    assert len(calls) == 1


def test_effective_local_context_hot_path_not_restatting(monkeypatch):
    """effective_local_context() must not re-derive the HF-cache tier on
    every call once it has been computed once."""
    calls = []

    def fake_hf_cache_config(repo):
        calls.append(repo)
        return {"max_position_embeddings": 12345}

    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", None)
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Qwen3-14B-4bit"])
    monkeypatch.setattr(R, "_hf_derived_config", R._HF_CONFIG_UNSET)
    monkeypatch.setattr(R, "_hf_cache_config", fake_hf_cache_config)

    assert R.effective_local_context() == 12345
    assert R.effective_local_context() == 12345
    assert len(calls) == 1


# --- precedence: explicit > probe > HF cache > legacy default ---------------

def test_precedence_explicit_beats_hf_cache(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 999)
    monkeypatch.setattr(R, "_derived_local_context", None)
    monkeypatch.setattr(R, "_hf_derived_config", {"max_position_embeddings": 40960})
    assert R.effective_local_context() == 999


def test_precedence_probe_beats_hf_cache(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", 8888)
    monkeypatch.setattr(R, "_hf_derived_config", {"max_position_embeddings": 40960})
    assert R.effective_local_context() == 8888


def test_precedence_hf_cache_beats_legacy_default(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", None)
    monkeypatch.setattr(R, "_hf_derived_config", {"max_position_embeddings": 40960})
    assert R.effective_local_context() == 40960


# --- bounded HF cache read ----------------------------------------------------
# A filesystem read can hang rather than fail: macOS TCC blocks a protected
# path while waiting for a consent prompt no launchd job can show, and an
# unreachable network mount sits in uninterruptible sleep. Neither raises, so
# try/except OSError never fires. _hf_cache_config bounds the read and treats
# an overrun exactly like a cache miss.

def test_hanging_read_returns_none_within_timeout(monkeypatch):
    started = threading.Event()

    def _never_returns(repo):
        started.set()
        time.sleep(30)          # simulates a TCC-blocked / dead-mount read
        return 999999           # must never reach the caller

    monkeypatch.setattr(R, "_read_hf_cache_config", _never_returns)
    monkeypatch.setattr(R, "HF_CACHE_READ_TIMEOUT", 0.2)

    t0 = time.monotonic()
    result = R._hf_cache_config("mlx-community/Whatever-4bit")
    elapsed = time.monotonic() - t0

    assert result is None
    assert started.is_set(), "worker should have actually started"
    assert elapsed < 5, f"gave up in {elapsed:.2f}s; must not wait on the read"


def test_hanging_read_worker_is_daemon_so_exit_is_not_blocked(monkeypatch):
    """A thread stuck in a syscall cannot be killed, so it is abandoned --
    daemon=True keeps the abandoned thread from holding up interpreter exit."""
    captured = {}
    real_thread = threading.Thread

    def _spy(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        captured["daemon"] = t.daemon
        return t

    monkeypatch.setattr(R, "_read_hf_cache_config", lambda repo: time.sleep(30))
    monkeypatch.setattr(R, "HF_CACHE_READ_TIMEOUT", 0.2)
    monkeypatch.setattr(threading, "Thread", _spy)

    R._hf_cache_config("mlx-community/Whatever-4bit")
    assert captured.get("daemon") is True


def test_fast_read_still_returns_its_value(monkeypatch):
    monkeypatch.setattr(R, "_read_hf_cache_config", lambda repo: 40960)
    monkeypatch.setattr(R, "HF_CACHE_READ_TIMEOUT", 5.0)
    assert R._hf_cache_config("mlx-community/Qwen3-14B-4bit") == 40960


def test_worker_exception_becomes_none_not_a_crash(monkeypatch):
    def _boom(repo):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(R, "_read_hf_cache_config", _boom)
    monkeypatch.setattr(R, "HF_CACHE_READ_TIMEOUT", 5.0)
    assert R._hf_cache_config("mlx-community/Qwen3-14B-4bit") is None


def test_timeout_is_cached_not_retried_per_request(monkeypatch):
    """A timeout is cached like any other miss. Retrying per request would
    stall the routing hot path by the timeout on every call."""
    calls = []

    def _slow(repo):
        calls.append(repo)
        time.sleep(30)

    monkeypatch.setattr(R, "_read_hf_cache_config", _slow)
    monkeypatch.setattr(R, "HF_CACHE_READ_TIMEOUT", 0.2)
    monkeypatch.setattr(R, "_hf_derived_config", R._HF_CONFIG_UNSET)
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Qwen3-14B-4bit"])
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", None)
    monkeypatch.setattr(R, "_derived_local_context", None)

    assert R.effective_local_context() == R.LEGACY_CONTEXT_DEFAULT
    assert R.effective_local_context() == R.LEGACY_CONTEXT_DEFAULT
    assert len(calls) == 1, f"read attempted {len(calls)}x; must be cached"


# --- divisor family drift -----------------------------------------------------
# effective_local_context() follows the served model automatically; the token
# estimator's divisor is pinned by hand to one tokenizer family. These cover
# the check that notices when the two have drifted apart.

def _serve_config(monkeypatch, cfg):
    """Pin the cached HF-cache config to `cfg` (None = nothing resolvable)."""
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Some-Model"])
    monkeypatch.setattr(R, "_hf_derived_config", cfg)


def test_model_type_read_from_config(tmp_path, monkeypatch):
    repo = "mlx-community/Qwen3-14B-4bit"
    _write_hf_config(tmp_path, monkeypatch, repo,
                     {"max_position_embeddings": 40960, "model_type": "Qwen3"})
    # normalized: config.json casing varies between repos
    assert R._config_model_type(R._hf_cache_config(repo)) == "qwen3"


def test_model_type_from_nested_text_config(tmp_path, monkeypatch):
    repo = "mlx-community/Qwen3.6-VL-30B"
    _write_hf_config(tmp_path, monkeypatch, repo,
                     {"text_config": {"model_type": "qwen3_vl"}})
    assert R._config_model_type(R._hf_cache_config(repo)) == "qwen3_vl"


def test_model_type_absent_is_none(tmp_path, monkeypatch):
    repo = "mlx-community/No-Type"
    _write_hf_config(tmp_path, monkeypatch, repo, {"max_position_embeddings": 8192})
    assert R._config_model_type(R._hf_cache_config(repo)) is None


def test_model_type_non_string_is_none():
    assert R._config_model_type({"model_type": 3}) is None
    assert R._config_model_type({"model_type": "  "}) is None
    assert R._config_model_type(None) is None


def test_family_match_true_when_served_family_is_the_calibration_family(monkeypatch):
    _serve_config(monkeypatch, {"model_type": R.ESTIMATE_DIVISOR_REF_MODEL_TYPE})
    assert R.local_model_family() == R.ESTIMATE_DIVISOR_REF_MODEL_TYPE
    assert R.divisor_family_match() is True


def test_family_match_false_on_a_different_family(monkeypatch):
    _serve_config(monkeypatch, {"model_type": "llama"})
    assert R.divisor_family_match() is False


def test_family_match_none_when_undeterminable(monkeypatch):
    """None is 'unknown', never 'no mismatch' — reporting an unresolvable
    family as agreement would hide exactly the case this check exists for."""
    _serve_config(monkeypatch, None)                       # nothing in the cache
    assert R.divisor_family_match() is None
    _serve_config(monkeypatch, {"max_position_embeddings": 8192})  # no model_type
    assert R.divisor_family_match() is None


def test_family_mismatch_never_changes_routing(monkeypatch):
    """The check reports; it must not inflate the divisor or force cloud.
    An estimate wrong by a family-sized factor is a recalibration job."""
    body = {"messages": [{"role": "user", "content": "a" * 400}]}
    _serve_config(monkeypatch, {"model_type": R.ESTIMATE_DIVISOR_REF_MODEL_TYPE})
    matched = R.estimate_prompt_tokens(body)
    _serve_config(monkeypatch, {"model_type": "llama"})
    assert R.divisor_family_match() is False
    assert R.estimate_prompt_tokens(body) == matched


def test_mismatch_logs_one_startup_line(monkeypatch, capsys):
    _serve_config(monkeypatch, {"model_type": "gemma3"})
    monkeypatch.setattr(R, "QUIET", False)
    R._warn_on_divisor_family_mismatch()
    out = capsys.readouterr().out
    assert "gemma3" in out and R.ESTIMATE_DIVISOR_REF_MODEL_TYPE in out


def test_no_startup_line_when_family_matches_or_is_unknown(monkeypatch, capsys):
    monkeypatch.setattr(R, "QUIET", False)
    _serve_config(monkeypatch, {"model_type": R.ESTIMATE_DIVISOR_REF_MODEL_TYPE})
    R._warn_on_divisor_family_mismatch()
    _serve_config(monkeypatch, None)
    R._warn_on_divisor_family_mismatch()
    assert capsys.readouterr().out == ""


def test_health_reports_the_divisor_and_the_served_family(monkeypatch):
    _serve_config(monkeypatch, {"model_type": "llama"})
    with TestClient(R.app) as client:
        block = client.get("/health").json()["estimate_divisor"]
    assert block["chars_per_token"] == R.ESTIMATE_CHARS_PER_TOKEN
    assert block["ref_tokenizer"] == R.ESTIMATE_DIVISOR_REF_TOKENIZER
    assert block["served_model_type"] == "llama"
    assert block["family_match"] is False
