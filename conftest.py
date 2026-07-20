"""Suite-wide isolation from ambient configuration.

`load_config()` resolves three ambient sources -- `LOXO_CONFIG`, `./loxo.toml`,
and `$XDG_CONFIG_HOME/loxo-llm-router/loxo.toml` (defaulting to `~/.config`) --
plus a dozen env vars. A real deployment populates that XDG path, so without
this fixture the suite's results depend on whether the machine running it
happens to have the router deployed, and the tests fail on exactly the developer
machines that use the thing.

This neutralizes all three lookups. Values baked into module globals at import
time can't be reached from here (fixtures run after collection imports the
module); test_routing.py's `routing_env` pins those separately.
"""

import os
import tempfile

# Bind the app's import-time SPEND singleton to a throwaway state dir. This
# must happen at conftest import (before collection imports the package) —
# the per-test fixture below runs too late to affect module-level globals.
# Set unconditionally: a developer's real LOXO_STATE_DIR must not leak in.
# The module-level reference to _temp_state_dir is load-bearing: it keeps the
# TemporaryDirectory alive for the whole test session (nothing else holds it)
# and lets it clean itself up at interpreter exit, instead of leaking a
# mkdtemp'd directory on every test run.
_temp_state_dir = tempfile.TemporaryDirectory(prefix="loxo-test-state-")
os.environ["LOXO_STATE_DIR"] = _temp_state_dir.name
os.environ["SPEND_LEDGER"] = ""  # and never write a ledger from the suite
os.environ["ADEQUACY_LEDGER"] = ""  # observe-only machinery must not write during tests

import pytest

# Every environment variable load_config() consults.
_CONFIG_ENV = (
    "LOXO_CONFIG",
    "ROUTER_NS",
    "HOST",
    "PORT",
    "LOXO_HOST",
    "LOXO_PORT",
    "LOCAL_BASE_URL",
    "CLOUD_BASE_URL",
    "LOCAL_MODELS",
    "LOCAL_CONTEXT_LIMIT",
    "CLOUD_DEFAULT_MODEL",
    "OPENROUTER_API_KEY",
    "ROUTER_TOKEN",
    "ROUTER_QUIET",
    # State (ledger) resolution — isolate like config, for the same reason
    # (module-level defaults above handle the import-time SPEND singleton;
    # this keeps per-test env clean).
    "SPEND_LEDGER",
    "LOXO_STATE_DIR",
    "XDG_STATE_HOME",
)


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Point every config source at somewhere empty, so only bundled defaults load."""
    for name in _CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)

    # Subdirectories, not tmp_path itself: tests that write their own loxo.toml
    # into tmp_path would otherwise have it picked up as the ambient ./loxo.toml.
    empty_cwd = tmp_path / "_empty_cwd"
    empty_xdg = tmp_path / "_empty_xdg"
    empty_cwd.mkdir()
    empty_xdg.mkdir()
    monkeypatch.chdir(empty_cwd)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_xdg))
    monkeypatch.setenv("LOXO_STATE_DIR", str(tmp_path / "_empty_state"))
