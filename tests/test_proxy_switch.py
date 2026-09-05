"""Integration tests for the whole proxy.

No real network access is involved: both the Anthropic and the Ollama
responses are faked with httpx.MockTransport.

プロキシ全体の結合テスト。実際のネットワークには一切アクセスせず、
httpx.MockTransport でAnthropic/Ollama双方の応答を偽装して検証する。
"""

from __future__ import annotations

import asyncio
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


#: Shape of Ollama's usage endpoint: 0-1 utilization ratios, and no reset times.
#: Ollamaの使用量エンドポイントの形。使用率は0〜1で、リセット時刻は返らない。
_OLLAMA_USAGE_BODY = {
    "limits": {
        "session": {"usage": 0.1, "models": [{"name": "glm-5.3-flash", "request_count": 6}]},
        "weekly": {"usage": 0.2, "models": [{"name": "glm-5.3-flash", "request_count": 2672}]},
    }
}


def _ollama_messages_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("authorization") == "Bearer test-ollama-key"
    assert "x-api-key" not in request.headers
    if request.url.path == "/api/usage":
        return httpx.Response(200, json=_OLLAMA_USAGE_BODY)
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={"id": "msg_ollama", "model": body.get("model"), "content": [{"type": "text", "text": "hi"}]},
    )


async def test_failover_then_recovery(settings: Settings) -> None:
    # 1st call = plenty of quota, 2nd = 5% left (below the 10% threshold) -> switch.
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

        # Observing this response's quota header (5% left) flips us to fallback.
        # このレスポンスのquotaヘッダー(残5%)を観測した結果、以後はfallbackになる
        r2 = await client.post("/v1/messages", json=payload, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["id"] == "msg_test"
        assert app.state.tracker.mode is BackendMode.FALLBACK

        # The 3rd call is routed to Ollama, with the model name mapped over.
        # 3回目はOllamaへルーティングされ、モデル名もマッピングされる
        r3 = await client.post("/v1/messages", json=payload, headers=headers)
        assert r3.status_code == 200
        body3 = r3.json()
        assert body3["id"] == "msg_ollama"
        assert body3["model"] == "gpt-oss:120b"

    assert backends.ollama_stats.requests_sent == 1
    assert backends.ollama_stats.requests_failed == 0
    await backends.aclose()


async def test_429_from_anthropic_fails_over_mid_session(settings: Settings) -> None:
    """Anthropic rejecting with 429 must count as exhaustion, headers or not.

    Anthropicの429には使用率ヘッダーが載らないため、これを枯渇として記録しないと
    残量取得不能のまま枯渇したAnthropicに留まり続ける。実際に起きた不具合では、
    Ollama側の残量が閾値未満でもAnthropicが底を突いていればOllamaへ逃がすのが正解。
    """
    calls = {"anthropic": 0, "ollama": 0}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        calls["anthropic"] += 1
        # 実障害と同じく、429応答にはunifiedヘッダーが一切付かない
        # Like the real incident, a 429 carries no unified headers at all
        return httpx.Response(429, json={"error": {"message": "rate limit exceeded"}})

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/usage":
            # 使用状況の読み取りは中継ではないため、中継数には数えない。
            # Reading usage is not a relay, so it must not count as one.
            return httpx.Response(200, json=_OLLAMA_USAGE_BODY)
        calls["ollama"] += 1
        return httpx.Response(
            200, json={"id": "msg_ollama", "model": "gpt-oss:120b", "content": []}
        )

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(anthropic_handler),
        ollama_transport=httpx.MockTransport(ollama_handler),
    )
    app = create_app(settings, backends=backends)

    payload = {
        "model": "claude-opus-4-20260101",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = {"x-api-key": "sk-ant-test", "anthropic-version": "2023-06-01"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1回目は429のままClaude Codeへ返す(モードはこの応答でFALLBACKへ切替)
        # The 429 itself is relayed; observing it flips the mode to fallback
        r1 = await client.post("/v1/messages", json=payload, headers=headers)
        assert r1.status_code == 429
        assert app.state.tracker.mode is BackendMode.FALLBACK
        assert app.state.tracker.last_reason == "anthropic_rate_limited"

        # 2回目(Claude Codeのリトライ相当)はOllamaへ流れる
        # The second call (Claude Code's retry) goes to Ollama
        r2 = await client.post("/v1/messages", json=payload, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["id"] == "msg_ollama"

    assert calls["anthropic"] == 1
    assert calls["ollama"] == 1
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
    # Force the app into fallback mode to reproduce the situation.
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
    """Claude Code health-checks with HEAD /api/hello, so it must be forwarded, not 405ed.

    Claude CodeはHEAD /api/helloで接続確認を行うため、405にせず転送できる必要がある。
    """
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


async def test_usage_endpoint_is_preferred_and_costs_no_quota(settings: Settings) -> None:
    """The probe reads the usage endpoint, not /v1/messages, when it answers.

    使用量エンドポイントが応答する限り、/v1/messages のプローブは使われないこと。
    """
    calls = {"usage": 0, "messages": 0}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth/usage":
            calls["usage"] += 1
            assert request.headers.get("authorization") == "Bearer oauth-token"
            assert request.headers.get("anthropic-beta") == "oauth-2025-04-20"
            return httpx.Response(200, json={"five_hour": {"utilization": 5.0}})
        calls["messages"] += 1
        return httpx.Response(200, json={"id": "msg"})

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(anthropic_handler),
        ollama_transport=httpx.MockTransport(_ollama_messages_handler),
    )
    app = create_app(settings, backends=backends)
    probe = app.state.recovery_probe
    probe.capture_auth_headers({"authorization": "Bearer oauth-token"})

    await probe._probe_once()

    assert calls["usage"] == 1
    assert calls["messages"] == 0
    # 残量95%を観測しているので通常モードのまま / 95% left, so it stays on Anthropic
    assert app.state.tracker.mode is BackendMode.ANTHROPIC
    assert app.state.tracker.last_snapshot.source == "usage_api"
    await backends.aclose()


async def test_falls_back_to_messages_probe_when_usage_endpoint_is_gone(settings: Settings) -> None:
    """A 404 disables the usage endpoint; in fallback we then pay for a probe.

    404なら以後は使用量エンドポイントを使わず、フォールバック中は従来プローブに落ちること。
    """
    calls = {"usage": 0, "messages": 0}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth/usage":
            calls["usage"] += 1
            return httpx.Response(404, json={"error": "not found"})
        calls["messages"] += 1
        return httpx.Response(
            200,
            headers={"anthropic-ratelimit-unified-5h-utilization": "0.5"},
            json={"id": "msg"},
        )

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(anthropic_handler),
        ollama_transport=httpx.MockTransport(_ollama_messages_handler),
    )
    app = create_app(settings, backends=backends)
    app.state.tracker.mode = BackendMode.FALLBACK
    probe = app.state.recovery_probe
    probe.capture_auth_headers({"authorization": "Bearer oauth-token"})

    await probe._probe_once()
    assert calls == {"usage": 1, "messages": 1}
    # 残量50%まで回復したので切り戻る / recovered to 50% left, so it switches back
    assert app.state.tracker.mode is BackendMode.ANTHROPIC

    # 404で無効化済みなので、2回目はもう叩かない / disabled after the 404, so never retried
    app.state.tracker.mode = BackendMode.FALLBACK
    await probe._probe_once()
    assert calls["usage"] == 1


async def test_api_key_auth_never_calls_the_oauth_endpoint(settings: Settings) -> None:
    """The usage endpoint is OAuth-only, so an API key must not trigger it.

    使用量エンドポイントはOAuth専用のため、APIキー認証では呼ばないこと。
    """
    calls = {"usage": 0, "messages": 0}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        key = "usage" if request.url.path == "/api/oauth/usage" else "messages"
        calls[key] += 1
        return httpx.Response(200, json={"id": "msg"})

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(anthropic_handler),
        ollama_transport=httpx.MockTransport(_ollama_messages_handler),
    )
    app = create_app(settings, backends=backends)
    probe = app.state.recovery_probe
    probe.capture_auth_headers({"x-api-key": "sk-ant-test"})

    await probe._probe_once()
    assert calls["usage"] == 0
    # 通常モードなので有料プローブも撃たない / normal mode, so no paid probe either
    assert calls["messages"] == 0
    await backends.aclose()


async def test_ollama_headroom_is_known_from_the_very_first_request(settings: Settings) -> None:
    """Ollama's usage must be read at once, not one probe interval later.

    The guard that refuses to route into an exhausted Ollama needs a reading to
    judge on. While there is none the proxy will happily fail over into a
    backend that is out, and on a freshly started process that window used to
    last a whole probe interval - which is exactly when a new install hits it.

    Ollamaの使用状況を、プローブ間隔を待たずに読むこと。
    「枯渇したOllamaへ送らない」ガードは判定材料となる観測値を必要とする。
    それが無い間、プロキシは枯渇したバックエンドへ平気で切り替えてしまう。
    起動直後のプロセスではこの無防備な時間がプローブ間隔まるごとであり、
    新規導入時にちょうど踏み抜くことになる。
    """
    # 間隔を長く取る。修正前はこの間ずっと観測値が無いままだった。
    # A long interval: before the fix, nothing was read for all of it.
    settings.quota.probe_interval_seconds = 300.0
    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(_anthropic_messages_handler([0.5])),
        ollama_transport=httpx.MockTransport(_ollama_messages_handler),
    )
    app = create_app(settings, backends=backends)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post(
            "/v1/messages",
            json={"model": "claude-opus-4-1", "max_tokens": 10, "messages": []},
            headers={"x-api-key": "sk-ant-test"},
        )

    # プローブはバックグラウンドタスクなので、完了する隙を与える。
    # The probe is a background task; give it room to finish.
    for _ in range(100):
        if app.state.tracker.ollama_snapshot is not None:
            break
        await asyncio.sleep(0.01)

    snapshot = app.state.tracker.ollama_snapshot
    assert snapshot is not None, "Ollamaの残量が最初のリクエスト時点で読めていない"
    assert snapshot.weekly_utilization == pytest.approx(0.2)
    await app.state.recovery_probe.stop()
    await backends.aclose()
