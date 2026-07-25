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
