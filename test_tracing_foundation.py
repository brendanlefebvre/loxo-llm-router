"""Tests for the OTel emitter foundation (Plan A): record() choke point + session_id."""

import asyncio

import loxo_llm_router as R
from loxo_llm_router import ledger


def _obs(**kw):
    base = dict(cls="main", classifier_version=1, requested_model="loxo/auto",
                route="cloud", served_model="z-ai/glm-5.2", reason="virtual-quality-best",
                stream=False)
    base.update(kw)
    return ledger.Observation(**base)


def test_record_fans_out_to_adequacy(monkeypatch):
    seen = []

    async def fake_write(obs):
        seen.append(obs)

    monkeypatch.setattr(R.ADEQUACY, "write", fake_write)
    o = _obs()

    async def drive():
        R.record(o)
        # record() scheduled a fire-and-forget task; drain it deterministically.
        await asyncio.gather(*list(R._BACKGROUND_TASKS))

    asyncio.run(drive())
    assert seen == [o]


def test_to_entry_includes_session_id():
    assert _obs(session_id="sess-1").to_entry()["session_id"] == "sess-1"
    assert _obs().to_entry()["session_id"] is None


def test_resolve_prefers_header():
    body = {"user": "u", "messages": [{"role": "user", "content": "hi"}]}
    assert R.resolve_session_id("hdr-1", body, "loxo/auto") == "hdr-1"


def test_resolve_falls_back_to_user_field():
    body = {"user": "user-42", "messages": [{"role": "user", "content": "hi"}]}
    assert R.resolve_session_id(None, body, "loxo/auto") == "user-42"
    assert R.resolve_session_id("", body, "loxo/auto") == "user-42"  # empty header ignored


def test_resolve_fingerprint_stable_and_sensitive():
    body = {"messages": [{"role": "system", "content": "SYS"},
                         {"role": "user", "content": "OPEN"}]}
    a = R.resolve_session_id(None, body, "loxo/auto")
    assert a == R.resolve_session_id(None, body, "loxo/auto")  # stable
    assert a.startswith("sys-")
    assert R.resolve_session_id(None, body, "loxo/other") != a  # model changes id
    body2 = {"messages": [{"role": "system", "content": "SYS"},
                          {"role": "user", "content": "DIFFERENT"}]}
    assert R.resolve_session_id(None, body2, "loxo/auto") != a  # first user msg changes id


def test_resolve_none_when_no_content():
    assert R.resolve_session_id(None, {}, "loxo/auto") is None
    assert R.resolve_session_id(None, {"messages": []}, "loxo/auto") is None


def test_resolve_never_raises_on_odd_shapes():
    body = {"user": 123,  # non-str user is ignored, not an error
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                         {"role": "system"}]}  # system msg with no content
    out = R.resolve_session_id(None, body, "loxo/auto")
    assert out.startswith("sys-")  # fingerprint over the user text "hi"


from fastapi.testclient import TestClient
from starlette.responses import JSONResponse


def _wire_client(monkeypatch):
    """A TestClient whose forward() is stubbed to capture the Observation."""
    captured = {}

    async def fake_forward(*args, **kwargs):
        captured["obs"] = kwargs.get("obs")
        return JSONResponse({"ok": True})

    monkeypatch.setattr(R, "forward", fake_forward)
    return TestClient(R.app), captured


def test_handler_wires_session_id_from_header(monkeypatch):
    client, captured = _wire_client(monkeypatch)
    resp = client.post(
        "/v1/chat/completions",
        headers={"x-loxo-session-id": "sess-abc"},
        json={"model": "loxo/auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert captured["obs"].session_id == "sess-abc"


def test_handler_wires_session_id_from_user_field(monkeypatch):
    client, captured = _wire_client(monkeypatch)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "loxo/auto", "user": "user-77",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert captured["obs"].session_id == "user-77"
