"""Hindsight and Groq clients against mocked transports (no network)."""

from __future__ import annotations

import json
import random

import httpx
import pytest

from app import hindsight, llm
from app.config import settings


def _hs(handler) -> hindsight.HindsightClient:
    return hindsight.HindsightClient(
        base_url="https://hs.test", api_key="k", bank_id="bank1", transport=httpx.MockTransport(handler)
    )


@pytest.mark.anyio
async def test_retain_uses_documented_path_auth_and_schema():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"success": True, "bank_id": "bank1", "items_count": 1, "async": False})

    client = _hs(handler)
    item = hindsight.build_memory_item(
        {
            "id": "mem_1",
            "grant_id": "G1",
            "spender": "A",
            "category": "Travel",
            "amount": 12.5,
            "vendor": "V",
            "content": "A spent 12.50",
            "timestamp": "2026-01-01T00:00:00Z",
        }
    )
    resp = await client.retain([item])
    assert resp.success and resp.items_count == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/default/banks/bank1/memories"
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body["async"] is False
    meta = body["items"][0]["metadata"]
    assert all(isinstance(v, str) for v in meta.values())
    assert meta["amount"] == "12.50"
    assert "grant:G1" in body["items"][0]["tags"]
    assert body["items"][0]["document_id"] == "mem_1"


@pytest.mark.anyio
async def test_retain_creates_missing_bank_then_retries():
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        posts = [c for c in calls if c[0] == "POST"]
        if req.method == "POST" and len(posts) == 1:
            return httpx.Response(404, json={"detail": "Bank 'bank1' not found"})
        if req.method == "PUT":
            return httpx.Response(200, json={"bank_id": "bank1"})
        return httpx.Response(200, json={"success": True, "bank_id": "bank1", "items_count": 1, "async": False})

    await _hs(handler).retain([hindsight.MemoryItem(content="x")])
    assert calls == [
        ("POST", "/v1/default/banks/bank1/memories"),
        ("PUT", "/v1/default/banks/bank1"),
        ("POST", "/v1/default/banks/bank1/memories"),
    ]


@pytest.mark.anyio
async def test_retain_5xx_raises_retryable_and_trips():
    client = _hs(lambda r: httpx.Response(503, text="down"))
    with pytest.raises(hindsight.HindsightError) as ei:
        await client.retain([hindsight.MemoryItem(content="x")])
    assert ei.value.retryable and ei.value.status_code == 503
    assert client.circuit_open


@pytest.mark.anyio
async def test_retain_401_is_not_retryable():
    client = _hs(lambda r: httpx.Response(401, json={"detail": "bad key"}))
    with pytest.raises(hindsight.HindsightError) as ei:
        await client.retain([hindsight.MemoryItem(content="x")])
    assert ei.value.retryable is False


@pytest.mark.anyio
async def test_recall_parses_results_and_filters_by_tag():
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert req.url.path == "/v1/default/banks/bank1/memories/recall"
        assert "top_k" not in body
        assert body["tags"] == ["grant:G1"] and body["tags_match"] == "any_strict"
        return httpx.Response(200, json={"results": [{"id": "h1", "text": "t", "metadata": {"amount": "5.00"}}]})

    res = await _hs(handler).recall("travel", tags=["grant:G1"])
    assert res[0].id == "h1" and res[0].metadata == {"amount": "5.00"}


@pytest.mark.anyio
async def test_recall_404_bank_returns_empty():
    res = await _hs(lambda r: httpx.Response(404, json={"detail": "nope"})).recall("q")
    assert res == []


@pytest.mark.anyio
async def test_malformed_recall_response_raises_non_retryable():
    with pytest.raises(hindsight.HindsightError) as ei:
        await _hs(lambda r: httpx.Response(200, json={"memories": []})).recall("q")
    assert ei.value.retryable is False


@pytest.mark.anyio
async def test_transport_error_trips_circuit():
    def handler(req):
        raise httpx.ConnectTimeout("slow")

    client = _hs(handler)
    with pytest.raises(hindsight.HindsightError):
        await client.recall("q")
    assert client.circuit_open
    with pytest.raises(hindsight.HindsightError, match="circuit open"):
        await client.recall("q")


@pytest.mark.anyio
async def test_delete_document_and_health():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(404, json={})

    client = _hs(handler)
    await client.delete_document("mem_1")  # 404 treated as already deleted
    assert (await client.health())["status"] == "ok"
    assert ("DELETE", "/v1/default/banks/bank1/documents/mem_1") in seen
    assert (await hindsight.HindsightClient(api_key="").health())["status"] == "disabled"


@pytest.mark.anyio
async def test_disabled_client_raises():
    with pytest.raises(hindsight.HindsightDisabled):
        await hindsight.HindsightClient(api_key="").recall("q")


def test_request_models_reject_bad_payloads():
    with pytest.raises(ValueError):
        hindsight.MemoryItem(content="x", metadata={"amount": 5})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        hindsight.RetainRequest(items=[])
    with pytest.raises(ValueError):
        hindsight.RecallRequest(query="")


def test_base_url_version_suffix_is_normalised():
    from app.config import Settings

    assert Settings(hindsight_base_url="https://api.hindsight.vectorize.io/v1/").hindsight_base_url == (
        "https://api.hindsight.vectorize.io"
    )


# ---------------------------------------------------------------- Groq ---- #


def _groq(handler, sleeps: list[float] | None = None, retries: int = 3) -> llm.GroqClient:
    async def fake_sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    return llm.GroqClient(
        api_key="g",
        base_url="https://groq.test/openai/v1",
        max_retries=retries,
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
        rng=random.Random(1),
    )


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.anyio
async def test_groq_json_mode_payload():
    def handler(req):
        body = json.loads(req.content)
        assert req.url.path == "/openai/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert req.headers["authorization"] == "Bearer g"
        return _ok('{"a": 1}')

    out = await _groq(handler).complete_json("Reply in JSON", "x")
    assert out.data == {"a": 1} and out.attempts == 1


@pytest.mark.anyio
async def test_groq_429_retries_with_backoff_and_retry_after(monkeypatch):
    monkeypatch.setattr(settings, "groq_backoff_base_seconds", 0.5)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "rl"}})
        return _ok('{"ok": true}')

    sleeps: list[float] = []
    out = await _groq(handler, sleeps).complete_json("json", "x")
    assert out.attempts == 3
    assert len(sleeps) == 2 and all(s >= 2.0 for s in sleeps)


@pytest.mark.anyio
async def test_groq_429_exhausts_retries():
    sleeps: list[float] = []
    with pytest.raises(llm.LLMError) as ei:
        await _groq(lambda r: httpx.Response(429, json={}), sleeps, retries=2).complete_json("json", "x")
    assert ei.value.kind == "rate_limited" and len(sleeps) == 2


@pytest.mark.anyio
async def test_groq_timeout_retried_then_raises():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        raise httpx.ReadTimeout("slow")

    with pytest.raises(llm.LLMError) as ei:
        await _groq(handler, retries=1).complete_json("json", "x")
    assert ei.value.kind == "timeout" and n["i"] == 2


@pytest.mark.anyio
async def test_groq_transport_error():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(llm.LLMError) as ei:
        await _groq(handler, retries=0).complete_json("json", "x")
    assert ei.value.kind == "transport"


@pytest.mark.anyio
async def test_groq_malformed_content_is_error():
    with pytest.raises(llm.LLMError) as ei:
        await _groq(lambda r: _ok("not json {")).complete_json("json", "x")
    assert ei.value.kind == "malformed"
    with pytest.raises(llm.LLMError):
        await _groq(lambda r: _ok("[1,2]")).complete_json("json", "x")
    with pytest.raises(llm.LLMError):
        await _groq(lambda r: httpx.Response(200, json={"choices": []})).complete_json("json", "x")


@pytest.mark.anyio
async def test_groq_json_validate_failed_retried_once():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(400, json={"error": {"code": "json_validate_failed", "failed_generation": "{"}})
        return _ok('{"x": 2}')

    out = await _groq(handler).complete_json("json", "x")
    assert out.data == {"x": 2} and out.attempts == 2


@pytest.mark.anyio
async def test_groq_non_retryable_4xx():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(llm.LLMError) as ei:
        await _groq(handler).complete_json("json", "x")
    assert ei.value.kind == "http" and n["i"] == 1


@pytest.mark.anyio
async def test_groq_disabled_without_key():
    with pytest.raises(llm.LLMError) as ei:
        await llm.GroqClient(api_key="").complete_json("json", "x")
    assert ei.value.kind == "disabled"


@pytest.mark.anyio
async def test_groq_ping():
    assert (await _groq(lambda r: httpx.Response(200, json={"data": []})).ping())["status"] == "ok"
    assert (await llm.GroqClient(api_key="").ping())["status"] == "disabled"


def test_backoff_is_bounded_full_jitter():
    rng = random.Random(0)
    for attempt in range(10):
        d = llm.backoff_delay(attempt, base=0.5, cap=8.0, rng=rng)
        assert 0.0 <= d <= min(8.0, 0.5 * 2**attempt)
