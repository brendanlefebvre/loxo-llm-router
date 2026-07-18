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
    yield


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
