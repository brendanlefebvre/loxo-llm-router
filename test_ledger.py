"""Tests for loxo_llm_router.ledger: state-dir resolution and the SpendTracker.

Pure filesystem + asyncio tests; no network, no server. The conftest isolates
config env vars; state env vars are handled per-test here.
"""

import asyncio
import json
import pathlib

import pytest

from loxo_llm_router import ledger


@pytest.fixture(autouse=True)
def state_env(monkeypatch, tmp_path):
    """Each test starts with no ambient state configuration."""
    monkeypatch.delenv("LOXO_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("SPEND_LEDGER", raising=False)
    # Point the legacy path somewhere empty by default; tests create it as needed.
    monkeypatch.setattr(ledger, "LEGACY_SPEND_LEDGER", tmp_path / "legacy" / "spend.jsonl")
    return


# --- state_dir ----------------------------------------------------------------

def test_state_dir_default_is_xdg_state_home_fallback():
    assert ledger.state_dir() == pathlib.Path.home() / ".local" / "state" / "loxo-llm-router"


def test_state_dir_honors_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert ledger.state_dir() == tmp_path / "xdg-state" / "loxo-llm-router"


def test_state_dir_loxo_state_dir_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("LOXO_STATE_DIR", str(tmp_path / "explicit"))
    assert ledger.state_dir() == tmp_path / "explicit"


# --- resolve_spend_ledger -----------------------------------------------------

def test_resolve_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEND_LEDGER", str(tmp_path / "custom.jsonl"))
    assert ledger.resolve_spend_ledger() == tmp_path / "custom.jsonl"


def test_resolve_empty_env_disables(monkeypatch):
    monkeypatch.setenv("SPEND_LEDGER", "")
    assert ledger.resolve_spend_ledger() is None


def test_resolve_defaults_to_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LOXO_STATE_DIR", str(tmp_path / "state"))
    assert ledger.resolve_spend_ledger() == tmp_path / "state" / "spend.jsonl"


def test_resolve_falls_back_to_existing_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("LOXO_STATE_DIR", str(tmp_path / "state"))  # new path absent
    legacy = ledger.LEGACY_SPEND_LEDGER
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"usd": 1}\n')
    logged = []
    assert ledger.resolve_spend_ledger(logged.append) == legacy
    assert any("legacy" in line for line in logged)  # pointer is logged


def test_resolve_prefers_new_path_when_it_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("LOXO_STATE_DIR", str(tmp_path / "state"))
    new = tmp_path / "state" / "spend.jsonl"
    new.parent.mkdir(parents=True)
    new.write_text("")
    legacy = ledger.LEGACY_SPEND_LEDGER
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"usd": 1}\n')
    assert ledger.resolve_spend_ledger() == new  # no silent history fork


# --- SpendTracker -------------------------------------------------------------

def _tracker(path: pathlib.Path | None) -> ledger.SpendTracker:
    return ledger.SpendTracker(path)


def test_tracker_seeds_from_ledger(tmp_path):
    f = tmp_path / "spend.jsonl"
    f.write_text(
        json.dumps({"ts": "2026-07-01T00:00:00+00:00", "provider": "openrouter.ai",
                    "model": "m1", "usd": 0.5}) + "\n"
        + "not json\n"                                    # tolerated
        + json.dumps({"provider": "openrouter.ai", "model": "m1", "usd": 0}) + "\n"  # skipped
        + json.dumps({"ts": "2026-06-01T00:00:00+00:00", "provider": "other",
                      "model": "m2", "usd": 0.25}) + "\n"
    )
    t = _tracker(f)
    snap = asyncio.run(t.snapshot())
    assert snap["total_usd"] == 0.75
    assert snap["requests"] == 2
    assert snap["since"] == "2026-06-01T00:00:00+00:00"  # earliest entry wins
    assert snap["by_provider"]["openrouter.ai"]["by_model"]["m1"]["usd"] == 0.5


def test_tracker_record_appends_and_accumulates(tmp_path):
    f = tmp_path / "sub" / "spend.jsonl"  # parent dir does not exist yet
    t = _tracker(f)
    asyncio.run(t.record("openrouter.ai", "m1", 0.1, stream=False, reason="test"))
    asyncio.run(t.record("openrouter.ai", "m1", 0.2, stream=True, reason="test"))
    lines = [json.loads(x) for x in f.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["usd"] == 0.1
    assert lines[0]["stream"] is False
    snap = asyncio.run(t.snapshot())
    assert snap["total_usd"] == pytest.approx(0.3)
    assert snap["by_provider"]["openrouter.ai"]["requests"] == 2


def test_tracker_zero_cost_is_noop(tmp_path):
    f = tmp_path / "spend.jsonl"
    t = _tracker(f)
    asyncio.run(t.record("p", "m", 0.0, stream=False, reason="test"))
    assert not f.exists()
    assert asyncio.run(t.snapshot())["requests"] == 0


def test_tracker_zero_cost_with_cache_stats_is_recorded(tmp_path):
    """A zero-cost response can still carry cache stats (e.g. a fully
    cache-hit request) — usd contributes 0 but the request and its cache
    stats must not be dropped."""
    f = tmp_path / "spend.jsonl"
    t = _tracker(f)
    asyncio.run(t.record("p", "m", 0.0, stream=False, reason="test",
                         cached_tokens=500, cache_savings_usd=0.001))
    assert f.exists()
    lines = [json.loads(x) for x in f.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["usd"] == 0.0
    assert lines[0]["cached_tokens"] == 500
    assert lines[0]["cache_savings_usd"] == 0.001
    snap = asyncio.run(t.snapshot())
    assert snap["total_usd"] == 0.0
    assert snap["requests"] == 1
    model_stats = snap["by_provider"]["p"]["by_model"]["m"]
    assert model_stats["cached_tokens"] == 500
    assert model_stats["est_cache_savings_usd"] == pytest.approx(0.001)


def test_tracker_disabled_ledger_memory_only():
    t = _tracker(None)
    asyncio.run(t.record("p", "m", 0.4, stream=False, reason="test"))
    assert asyncio.run(t.snapshot())["total_usd"] == 0.4


def test_tracker_write_failure_never_raises(tmp_path):
    blocked = tmp_path / "as-dir"
    blocked.mkdir()  # opening a directory for append fails
    t = _tracker(blocked)
    asyncio.run(t.record("p", "m", 0.4, stream=False, reason="test"))  # must not raise
    assert asyncio.run(t.snapshot())["total_usd"] == 0.4  # still counted in memory


def test_tracker_seed_failure_starts_fresh(tmp_path):
    unreadable = tmp_path / "dir-as-ledger"
    unreadable.mkdir()
    t = _tracker(unreadable)  # read_text on a dir raises internally; must not propagate
    assert asyncio.run(t.snapshot())["total_usd"] == 0.0


# --- suite isolation canary ---------------------------------------------------

def test_app_spend_singleton_is_isolated_from_real_state():
    """conftest's module-level env must have bound the import-time SPEND
    singleton away from any real ledger before the package was imported."""
    import loxo_llm_router as R
    assert R.SPEND.ledger_path is None  # SPEND_LEDGER="" disables persistence


# --- B1: cache stats on the spend tracker -------------------------------------

def test_record_accumulates_cache_stats(tmp_path):
    f = tmp_path / "spend.jsonl"
    t = _tracker(f)
    asyncio.run(t.record("p", "m", 0.1, stream=False, reason="r",
                         cached_tokens=1000, cache_savings_usd=0.002))
    asyncio.run(t.record("p", "m", 0.1, stream=False, reason="r"))
    snap = asyncio.run(t.snapshot())
    m = snap["by_provider"]["p"]["by_model"]["m"]
    assert m["cached_tokens"] == 1000
    assert m["est_cache_savings_usd"] == 0.002
    entries = [json.loads(x) for x in f.read_text().splitlines()]
    assert entries[0]["cached_tokens"] == 1000
    assert "cached_tokens" not in entries[1]  # zero -> omitted, entries stay lean


def test_seed_tolerates_pre_cache_entries(tmp_path):
    f = tmp_path / "spend.jsonl"
    f.write_text(json.dumps({"provider": "p", "model": "m", "usd": 0.5}) + "\n")
    snap = asyncio.run(_tracker(f).snapshot())
    assert snap["by_provider"]["p"]["by_model"]["m"]["cached_tokens"] == 0
