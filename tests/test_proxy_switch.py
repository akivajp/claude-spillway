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


#: Ollama's model listing. Like the usage endpoint it is a read, not a relay,
#: so every handler below answers it without counting it.
#: Ollamaのモデル一覧。使用量エンドポイントと同様に「読み取り」であって中継では
#: ないため、以下のハンドラはこれを中継数に数えずに応答する。
_OLLAMA_TAGS_BODY = {"models": [{"name": "gpt-oss:120b"}, {"name": "glm-5.3-flash"}]}


def _ollama_messages_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("authorization") == "Bearer test-ollama-key"
    assert "x-api-key" not in request.headers
    if request.url.path == "/api/tags":
        return httpx.Response(200, json=_OLLAMA_TAGS_BODY)
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
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_OLLAMA_TAGS_BODY)
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


#: Ollamaの週次枠を使い切ったアカウントの応答。この状態で中継すると429が返る。
#: An account whose weekly Ollama allowance is gone; relaying there returns 429.
_OLLAMA_USAGE_EXHAUSTED = {
    "limits": {
        "session": {"usage": 0.1, "models": []},
        "weekly": {"usage": 1.0, "models": []},
    }
}


async def test_never_relays_into_an_exhausted_ollama_when_anthropic_rejects(
    settings: Settings,
) -> None:
    """A 429 from Anthropic must not push traffic into an Ollama that is out.

    The reported failure: on a fresh start every session opened with a burst of
    Ollama's "weekly usage limit" 429s before an answer finally arrived. A
    single Anthropic 429 pinned it to zero, which tied with Ollama's measured
    zero, and the tie sent traffic to the backend that was certainly out rather
    than the one that was merely suspected.

    Anthropicの429で、枯渇済みのOllamaへトラフィックを送らないこと。
    報告された不具合: 新規起動のたびにセッション冒頭でOllamaの「週次上限」429が
    連発し、しばらくしてようやく応答が返っていた。Anthropicの429がAnthropicを
    0扱いにし、それがOllamaの実測0%と同値になり、同値のときに「確実に枯渇して
    いる方」を選んでいたことが原因。
    """
    calls = {"anthropic": 0, "ollama_messages": 0}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        calls["anthropic"] += 1
        # 短期レート制限の429。使用率ヘッダーは載らない。
        # A short-term rate limit: no utilization headers on it.
        return httpx.Response(429, json={"error": {"message": "rate limit exceeded"}})

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_OLLAMA_TAGS_BODY)
        if request.url.path == "/api/usage":
            return httpx.Response(200, json=_OLLAMA_USAGE_EXHAUSTED)
        calls["ollama_messages"] += 1
        return httpx.Response(429, json={"error": {"message": "weekly usage limit"}})

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(anthropic_handler),
        ollama_transport=httpx.MockTransport(ollama_handler),
    )
    app = create_app(settings, backends=backends)

    payload = {"model": "claude-opus-4-1", "max_tokens": 10, "messages": []}
    headers = {"x-api-key": "sk-ant-test"}

    # lifespanを通す。起動時のOllama残量読み取りはここで走る。
    # Run the lifespan: the startup read of Ollama's headroom happens here.
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.tracker.ollama_snapshot is not None:
                break
            await asyncio.sleep(0.01)
        assert app.state.tracker.ollama_snapshot is not None, (
            "起動時点でOllamaの残量が読めていない"
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Claude Codeのリトライを模して数回叩く。
            # Several calls, standing in for Claude Code's retries.
            for _ in range(5):
                response = await client.post("/v1/messages", json=payload, headers=headers)
                # Anthropicの429がそのまま返る。これは正直な応答であり、
                # Ollamaの「週次上限」429にすり替わってはならない。
                # Anthropic's own 429 is relayed; it must never be replaced by
                # Ollama's "weekly usage limit" one.
                assert response.status_code == 429
                assert "weekly usage limit" not in response.text

    assert calls["ollama_messages"] == 0, "枯渇済みのOllamaへ中継してしまっている"
    assert app.state.tracker.mode is BackendMode.ANTHROPIC
    assert app.state.tracker.last_reason == "ollama_exhausted"


async def test_a_429_that_still_reports_headroom_is_not_exhaustion(settings: Settings) -> None:
    """A rejection while the windows still report room is a short-term limit.

    窓に余裕があることを同じ応答が示している429は、短期レート制限である。
    """

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "anthropic-ratelimit-unified-5h-utilization": "0.13",
                "anthropic-ratelimit-unified-7d-utilization": "0.01",
            },
            json={"error": {"message": "rate limit exceeded"}},
        )

    calls = {"ollama": 0}

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_OLLAMA_TAGS_BODY)
        if request.url.path == "/api/usage":
            return httpx.Response(200, json=_OLLAMA_USAGE_BODY)
        calls["ollama"] += 1
        return httpx.Response(200, json={"id": "msg_ollama", "content": []})

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(anthropic_handler),
        ollama_transport=httpx.MockTransport(ollama_handler),
    )
    app = create_app(settings, backends=backends)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(3):
            assert (
                await client.post(
                    "/v1/messages",
                    json={"model": "claude-opus-4-1", "max_tokens": 10, "messages": []},
                    headers={"x-api-key": "sk-ant-test"},
                )
            ).status_code == 429

    assert app.state.tracker.mode is BackendMode.ANTHROPIC
    assert calls["ollama"] == 0
    await app.state.recovery_probe.stop()


#: ollama.com の設定ページから読み取り対象の要素だけを残したもの。
#: The ollama.com settings page, trimmed to the elements that are read.
_OLLAMA_SETTINGS_PAGE = """
<div data-usage-track="" aria-label="Session usage 10% used"></div>
<div class="text-xs local-time" data-time="2026-10-14T15:00:00Z">Resets in 4 hours.</div>
<div data-usage-track="" aria-label="Weekly usage 20% used">
  <button type="button" data-usage-segment="" data-model="glm-5.3-flash" data-requests="157"></button>
</div>
<div class="text-xs local-time" data-time="2026-10-19T00:00:00Z">Resets in 4 days.</div>
"""


def _ollama_web_backends(settings: Settings, page_handler) -> ProxyBackends:
    """設定ページへの応答だけを差し替えたバックエンドを作る。

    Backends whose Ollama side answers the settings page with ``page_handler``.
    """

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/settings":
            return page_handler(request)
        return _ollama_messages_handler(request)

    return ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        ollama_transport=httpx.MockTransport(ollama_handler),
    )


async def test_session_cookie_reads_quota_from_the_settings_page(settings: Settings) -> None:
    """With a session cookie, utilization and exact resets come from /settings.

    セッションCookieがあれば、使用率と正確なリセット時刻を /settings から読むこと。
    ページは間隔を空けて読み、毎ティックは叩かないこと。
    """
    settings.ollama.session_cookie = "__Secure-session=abc"
    calls = {"page": 0}

    def page_handler(request: httpx.Request) -> httpx.Response:
        calls["page"] += 1
        # 「名前=値」で設定しても、Cookieヘッダーは1組だけになる
        # Pasting "name=value" still yields a single cookie pair
        assert request.headers.get("cookie") == "__Secure-session=abc"
        return httpx.Response(200, text=_OLLAMA_SETTINGS_PAGE)

    backends = _ollama_web_backends(settings, page_handler)
    app = create_app(settings, backends=backends)
    probe = app.state.recovery_probe
    probe.capture_auth_headers({"x-api-key": "sk-ant-test"})

    await probe._probe_once()
    await probe._probe_once()
    assert calls["page"] == 1

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        data = (await client.get("/_spillway/status")).json()["ollama"]
    assert data["session_utilization"] == pytest.approx(0.1)
    assert data["weekly_utilization"] == pytest.approx(0.2)
    assert data["session_reset"] is not None
    assert data["weekly_reset"] is not None
    assert data["usage_problem"] is None
    await backends.aclose()


async def test_expired_session_cookie_is_reported_once(settings: Settings) -> None:
    """A bounce to sign-in is reported as session_expired and not retried.

    サインインへの転送は session_expired として報告し、以後は読みに行かないこと。
    """
    settings.ollama.session_cookie = "stale"
    settings.ollama.usage_page_interval_seconds = 0.0
    calls = {"page": 0}

    def page_handler(request: httpx.Request) -> httpx.Response:
        calls["page"] += 1
        return httpx.Response(303, headers={"location": "https://ollama.com/signin"})

    backends = _ollama_web_backends(settings, page_handler)
    app = create_app(settings, backends=backends)
    probe = app.state.recovery_probe
    probe.capture_auth_headers({"x-api-key": "sk-ant-test"})

    await probe._probe_once()
    await probe._probe_once()
    assert calls["page"] == 1
    assert probe.ollama_usage_problem == "session_expired"
    assert app.state.tracker.ollama_snapshot is None
    await backends.aclose()


async def test_usage_api_without_utilization_is_reported(settings: Settings) -> None:
    """Without a cookie, the post-2026-10 API shape is reported, not ignored silently.

    Cookie未設定で、2026-10以降の形(リクエスト数のみ)が返る場合は黙って捨てず
    理由を報告し、以後はエンドポイントを叩かないこと。
    """
    calls = {"usage": 0}

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/usage":
            calls["usage"] += 1
            return httpx.Response(
                200, json={"range": "7d", "scope": "self", "totals": {"request_count": 473}, "buckets": []}
            )
        return _ollama_messages_handler(request)

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        ollama_transport=httpx.MockTransport(ollama_handler),
    )
    app = create_app(settings, backends=backends)
    probe = app.state.recovery_probe
    probe.capture_auth_headers({"x-api-key": "sk-ant-test"})

    await probe._probe_once()
    await probe._probe_once()
    assert calls["usage"] == 1
    assert probe.ollama_usage_problem == "api_no_utilization"
    await backends.aclose()


async def test_saved_session_is_reread_and_a_new_cookie_clears_expiry(settings: Settings) -> None:
    """A cookie refreshed by ollama-login is used at once, with no restart.

    ollama-login で更新されたCookieは再起動なしで即座に使われ、失効表示も消えること。
    保存ファイルの有効期限はステータスに載ること。
    """
    from claude_spillway.config import save_ollama_session

    session_file = settings.ollama.session_file_path()
    save_ollama_session(session_file, "stale", 1800000000.0)
    sent: list[str | None] = []

    def page_handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("cookie"))
        if request.headers.get("cookie") == "__Secure-session=stale":
            return httpx.Response(303, headers={"location": "https://ollama.com/signin"})
        return httpx.Response(200, text=_OLLAMA_SETTINGS_PAGE)

    backends = _ollama_web_backends(settings, page_handler)
    app = create_app(settings, backends=backends)
    probe = app.state.recovery_probe
    probe.capture_auth_headers({"x-api-key": "sk-ant-test"})

    await probe._probe_once()
    assert probe.ollama_usage_problem == "session_expired"
    # 同じCookieでは読みに行かない / the same stale cookie is not retried
    await probe._probe_once()
    assert len(sent) == 1

    # 取得間隔(120秒)の途中でも、新しいCookieなら即座に読む
    # A new cookie is read at once, even inside the 120s interval
    save_ollama_session(session_file, "fresh", 1900000000.0)
    await probe._probe_once()
    assert sent[-1] == "__Secure-session=fresh"
    assert probe.ollama_usage_problem is None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        data = (await client.get("/_spillway/status")).json()["ollama"]
    assert data["session_expires"] == 1900000000.0
    assert data["weekly_utilization"] == pytest.approx(0.2)
    await backends.aclose()
