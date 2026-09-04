"""プロキシ全体の結合テスト。実際のネットワークには一切アクセスせず、
httpx.MockTransport でAnthropic/Ollama双方の応答を偽装して検証する。
"""

from __future__ import annotations

import json

import httpx
import pytest

from claude_spillway.backends import ProxyBackends
from claude_spillway.config import Settings
from claude_spillway.proxy_app import create_app
from claude_spillway.quota import BackendMode


@pytest.fixture
def settings() -> Settings:
    s = Settings.load(None)
    s.quota.fallback_threshold_pct = 10.0
    s.quota.recovery_threshold_pct = 20.0
    s.ollama.api_key = "test-ollama-key"
    s.model_mapping.default = "gpt-oss:120b"
    return s


def _anthropic_messages_handler(utilizations: list[float]):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(calls["n"], len(utilizations) - 1)
        util = utilizations[idx]
        calls["n"] += 1
        if request.url.path == "/v1/messages":
            return httpx.Response(
                200,
                headers={"anthropic-ratelimit-unified-5h-utilization": str(util)},
                json={"id": "msg_test", "content": [{"type": "text", "text": "hello from anthropic"}]},
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    return handler


def _ollama_messages_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("authorization") == "Bearer test-ollama-key"
    assert "x-api-key" not in request.headers
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={"id": "msg_ollama", "model": body.get("model"), "content": [{"type": "text", "text": "hi"}]},
    )


async def test_failover_then_recovery(settings: Settings) -> None:
    # 1回目=十分な残量、2回目=残5%(閾値10%未満)で切替が起きる
    anthropic_transport = httpx.MockTransport(_anthropic_messages_handler([0.5, 0.95, 0.95]))
    ollama_transport = httpx.MockTransport(_ollama_messages_handler)
    backends = ProxyBackends(settings, anthropic_transport=anthropic_transport, ollama_transport=ollama_transport)
    app = create_app(settings, backends=backends)

    payload = {
        "model": "claude-opus-4-20260101",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = {"x-api-key": "sk-ant-test", "anthropic-version": "2023-06-01"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r1 = await client.post("/v1/messages", json=payload, headers=headers)
        assert r1.status_code == 200
        assert r1.json()["id"] == "msg_test"
        assert app.state.tracker.mode is BackendMode.ANTHROPIC

        # このレスポンスのquotaヘッダー(残5%)を観測した結果、以後はfallbackになる
        r2 = await client.post("/v1/messages", json=payload, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["id"] == "msg_test"
        assert app.state.tracker.mode is BackendMode.FALLBACK

        # 3回目はOllamaへルーティングされ、モデル名もマッピングされる
        r3 = await client.post("/v1/messages", json=payload, headers=headers)
        assert r3.status_code == 200
        body3 = r3.json()
        assert body3["id"] == "msg_ollama"
        assert body3["model"] == "gpt-oss:120b"

    assert backends.ollama_stats.requests_sent == 1
    assert backends.ollama_stats.requests_failed == 0
    await backends.aclose()


async def test_count_tokens_never_routed_to_ollama_even_in_fallback(settings: Settings) -> None:
    calls = {"anthropic": 0, "ollama": 0}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        calls["anthropic"] += 1
        return httpx.Response(200, json={"input_tokens": 5})

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        calls["ollama"] += 1
        return httpx.Response(200, json={"should": "not-be-called"})

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(anthropic_handler),
        ollama_transport=httpx.MockTransport(ollama_handler),
    )
    app = create_app(settings, backends=backends)
    # 既にフォールバック中であることを強制的に再現する
    app.state.tracker.mode = BackendMode.FALLBACK

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "claude-opus-4-20260101", "messages": []},
        )
        assert r.status_code == 200

    assert calls["anthropic"] == 1
    assert calls["ollama"] == 0
    await backends.aclose()


async def test_status_endpoint_reports_mode(settings: Settings) -> None:
    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(_anthropic_messages_handler([0.5])),
        ollama_transport=httpx.MockTransport(_ollama_messages_handler),
    )
    app = create_app(settings, backends=backends)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/_spillway/status")
        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "anthropic"
        assert data["thresholds"]["fallback_pct"] == 10.0
        assert data["ollama"]["requests_sent"] == 0

    await backends.aclose()


async def test_head_healthcheck_is_forwarded_to_anthropic(settings: Settings) -> None:
    """Claude CodeはHEAD /api/helloで接続確認を行うため、405にせず転送できる必要がある。"""
    calls = {"anthropic": 0}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        calls["anthropic"] += 1
        assert request.method == "HEAD"
        return httpx.Response(200)

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(anthropic_handler),
        ollama_transport=httpx.MockTransport(_ollama_messages_handler),
    )
    app = create_app(settings, backends=backends)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.head("/api/hello")
        assert r.status_code == 200

    assert calls["anthropic"] == 1
    await backends.aclose()
