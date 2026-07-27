"""record() fans a finalized obs out to the trace emitter."""

import asyncio

import loxo_llm_router as R
from loxo_llm_router import ledger


def _obs():
    return ledger.Observation(cls="main", classifier_version=1,
                              requested_model="loxo/auto", route="local",
                              served_model="qwen3-30b", reason="virtual-local",
                              stream=False, latency_ms=10)


def test_record_calls_trace_emit(monkeypatch):
    seen = []
    monkeypatch.setattr(R.TRACES, "emit", lambda o: seen.append(o))

    async def fake_write(obs):  # keep the adequacy sink quiet
        return None

    monkeypatch.setattr(R.ADEQUACY, "write", fake_write)
    o = _obs()

    async def drive():
        R.record(o)  # _spawn needs a running loop; TRACES.emit is sync (fires now)
        await asyncio.gather(*list(R._BACKGROUND_TASKS))

    asyncio.run(drive())
    assert seen == [o]
