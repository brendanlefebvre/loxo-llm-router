"""forward() records obs.fallback_at_ms when a transport fallback fires."""

import asyncio
import json

import httpx
import loxo_llm_router as R
from loxo_llm_router import ledger


_CLOUD_MS = 60  # simulated cloud-retry latency; long enough to place the pivot


class _Resp:
    def __init__(self, status=200, content=b'{"choices":[]}'):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": "application/json"}


def test_nonstreaming_fallback_sets_fallback_at_ms(monkeypatch):
    # First POST (local) raises a transport error; the retry (cloud) succeeds.
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("local down")
            await asyncio.sleep(_CLOUD_MS / 1000)  # the cloud retry takes real time
            return _Resp()

    monkeypatch.setattr(R.httpx, "AsyncClient", FakeClient)
    obs = ledger.Observation(cls="main", classifier_version=1,
                             requested_model="loxo/auto", route="local",
                             served_model="qwen3-30b", reason="virtual-local",
                             stream=False)
    monkeypatch.setattr(R, "record", lambda o: None)  # isolate: don't hit sinks

    asyncio.run(R.forward(
        primary_url=R.LOCAL_BASE_URL, path="/chat/completions",
        primary_body=json.dumps({"model": "x"}).encode(),
        client_headers={}, stream=False,
        fallback_url=R.CLOUD_BASE_URL, fallback_body=json.dumps({"model": "y"}).encode(),
        cloud_model=None, reason="virtual-local", obs=obs))

    assert obs.fallback_fired is True
    assert obs.fallback_at_ms is not None
    assert obs.fallback_at_ms >= 0
    # The marker is the PIVOT, not the finish: it must land before the cloud
    # retry's duration, else the fallback child span degenerates into a sliver
    # at the tail of the root instead of covering the retry.
    assert obs.latency_ms >= _CLOUD_MS
    assert obs.fallback_at_ms < _CLOUD_MS / 2
