"""Derived local-context threshold: explicit config > startup probe of
{LOCAL_BASE_URL}/models > legacy 60000 default. A down local server must
never block routing (probe failure -> cache stays None -> fallback)."""
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
