"""FastAPI app that Claude Code points ``ANTHROPIC_BASE_URL`` at.

Claude Codeから見て ``ANTHROPIC_BASE_URL`` の向き先となるFastAPIアプリ。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .backends import ProxyBackends, to_streaming_response
from .config import Settings
from .quota import BackendMode, QuotaTracker, RoutingPolicy
from .recovery import RecoveryProbe

logger = logging.getLogger("claude_spillway.proxy")

#: The one and only endpoint subject to failover. count_tokens, model listing
#: and friends always go to Anthropic because Ollama's compatibility shim is
#: unstable for them (ollama/ollama#13949).
#:
#: フェイルオーバーの対象とする唯一のエンドポイント。
#: count_tokens やモデル一覧取得等はOllamaの互換シムが不安定なため
#: (ollama/ollama#13949) 常にAnthropicへ流す。
_FAILOVER_PATH = "v1/messages"

_STARTED_AT = time.time()


def _resolve_policy(name: str) -> RoutingPolicy:
    """Map a configured policy name onto :class:`RoutingPolicy`, warning if unknown.

    設定されたポリシー名を :class:`RoutingPolicy` に対応付ける。未知なら警告する。
    """
    try:
        return RoutingPolicy(name)
    except ValueError:
        logger.warning(
            "unknown routing.policy %r; falling back to %s",
            name,
            RoutingPolicy.ANTHROPIC_FIRST.value,
        )
        return RoutingPolicy.ANTHROPIC_FIRST


def create_app(settings: Settings, backends: ProxyBackends | None = None) -> FastAPI:
    """Build the app. ``backends`` is exposed so tests can inject a mock.

    アプリを構築する。``backends`` はテストからモック注入するために公開している。
    """
    backends = backends or ProxyBackends(settings)
    routing = settings.routing
    tracker = QuotaTracker(
        fallback_threshold_pct=settings.quota.fallback_threshold_pct,
        recovery_threshold_pct=settings.quota.recovery_threshold_pct,
        # 未知のポリシー名は既定(anthropic_first)に倒す。設定ミスで起動できなく
        # なるより、従来どおりの安全側で動く方が望ましい。
        # An unknown policy name falls back to the default: running with the
        # original behaviour beats refusing to start over a config typo.
        policy=_resolve_policy(routing.policy),
        ollama_min_remaining_pct=routing.ollama_min_remaining_pct,
        balance_session_floor_pct=routing.balance_session_floor_pct,
        balance_margin_pct=routing.balance_margin_pct,
        anthropic_priority_weight=routing.anthropic_priority_weight,
        ollama_failure_threshold=routing.ollama_failure_threshold,
        reverse_failover_min_5h_pct=routing.reverse_failover_min_5h_pct,
        reverse_failover_cooldown_seconds=routing.reverse_failover_cooldown_seconds,
    )
    recovery_probe = RecoveryProbe(settings, backends, tracker)

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        # Shut down the background probe and the HTTP clients on exit.
        # 終了時にバックグラウンドプローブとHTTPクライアントを片付ける。
        await recovery_probe.stop()
        await backends.aclose()

    app = FastAPI(title="claude-spillway", lifespan=_lifespan)
    app.state.backends = backends
    app.state.tracker = tracker
    app.state.recovery_probe = recovery_probe
    app.state.settings = settings

    @app.get("/_spillway/status")
    async def status() -> JSONResponse:
        """Monitoring endpoint (used by the TUI) reporting mode and quota readings.

        現在のバックエンドモードやquota観測値を返す監視用エンドポイント(TUIから利用)。
        """
        snapshot = tracker.last_snapshot
        ollama_snapshot = tracker.ollama_snapshot
        stats = backends.ollama_stats
        return JSONResponse(
            {
                "mode": tracker.mode.value,
                # なぜ今このモードなのか。切替の理由を後から追えるようにする。
                # Why the current mode was chosen, so a switch can be explained.
                "reason": tracker.last_reason,
                "policy": tracker.policy.value,
                "uptime_seconds": time.time() - _STARTED_AT,
                "last_switch_at": tracker.last_switch_at,
                "thresholds": {
                    "fallback_pct": settings.quota.fallback_threshold_pct,
                    "recovery_pct": settings.quota.recovery_threshold_pct,
                },
                "anthropic": {
                    "utilization_5h": snapshot.utilization_5h if snapshot else None,
                    "utilization_7d": snapshot.utilization_7d if snapshot else None,
                    "requests_remaining_ratio": snapshot.requests_remaining_ratio if snapshot else None,
                    "tokens_remaining_ratio": snapshot.tokens_remaining_ratio if snapshot else None,
                    "remaining_ratio": snapshot.remaining_ratio() if snapshot else None,
                    "observed_at": snapshot.observed_at if snapshot else None,
                    # 各ウィンドウのリセット時刻(エポック秒)と、この観測値の取得元。
                    # When each window resets (epoch seconds), and where the reading came from.
                    "reset_5h": snapshot.reset_5h if snapshot else None,
                    "reset_7d": snapshot.reset_7d if snapshot else None,
                    "source": snapshot.source if snapshot else None,
                },
                "ollama": {
                    # Account-wide usage, read from Ollama's own usage endpoint.
                    # It reports no reset times, so those stay absent here.
                    # Ollama自身の使用量エンドポイントから読んだアカウント全体の
                    # 使用状況。リセット時刻は返らないためここにも含まれない。
                    "session_utilization": (
                        ollama_snapshot.session_utilization if ollama_snapshot else None
                    ),
                    "weekly_utilization": (
                        ollama_snapshot.weekly_utilization if ollama_snapshot else None
                    ),
                    "remaining_ratio": ollama_snapshot.remaining_ratio() if ollama_snapshot else None,
                    "observed_at": ollama_snapshot.observed_at if ollama_snapshot else None,
                    # 予測リセット時刻。利用率の上昇から求めた「リセットが確実に
                    # 済んでいる上限」であり、真の値はこれより早い可能性がある。
                    # Estimated resets: an upper bound derived from utilization
                    # rises. The true reset may be much earlier than either.
                    "estimated_session_reset": (
                        ollama_snapshot.estimated_session_reset if ollama_snapshot else None
                    ),
                    "estimated_weekly_reset": (
                        ollama_snapshot.estimated_weekly_reset if ollama_snapshot else None
                    ),
                    "weekly_models": (
                        [{"name": n, "request_count": c} for n, c in ollama_snapshot.weekly_models]
                        if ollama_snapshot
                        else []
                    ),
                    # このプロキシが中継した分だけの自己計測値。
                    # Self-measured counters covering only what this proxy relayed.
                    "requests_sent": stats.requests_sent,
                    "requests_failed": stats.requests_failed,
                    "consecutive_failures": stats.consecutive_failures,
                    "last_request_at": stats.last_request_at,
                    "last_status_code": stats.last_status_code,
                    "last_error": stats.last_error,
                },
            }
        )

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def proxy(full_path: str, request: Request) -> StreamingResponse:
        body = await request.body()
        header_map = {k.lower(): v for k, v in request.headers.items()}

        is_failover_target = request.method == "POST" and full_path == _FAILOVER_PATH

        if not is_failover_target or tracker.mode is BackendMode.ANTHROPIC:
            recovery_probe.capture_auth_headers(header_map)
            response, snapshot = await backends.forward_to_anthropic(request, body)
            if is_failover_target:
                previous_mode = tracker.mode
                new_mode = tracker.observe(snapshot)
                if new_mode is not previous_mode:
                    ratio = snapshot.remaining_ratio() or 0.0
                    logger.warning(
                        "quota running low (remaining=%.1f%%); switching backend %s -> %s",
                        ratio * 100,
                        previous_mode.value,
                        new_mode.value,
                    )
                    recovery_probe.start()
                # A 429 carries no utilization headers, so the observe() above
                # learned nothing. Record the rejection itself, or the proxy
                # stays parked on a backend that refuses every request.
                # 429には使用率ヘッダーが載らないため、上のobserve()では何も学べない。
                # 拒否そのものを記録しないと、全リクエストを拒否し続けるバックエンドに
                # 留まり続けてしまう。
                if response.status_code == 429:
                    new_mode = tracker.note_anthropic_rate_limited()
                    if new_mode is not previous_mode:
                        logger.warning(
                            "anthropic returned 429; switching backend %s -> %s",
                            previous_mode.value,
                            new_mode.value,
                        )
                        recovery_probe.start()
            return to_streaming_response(response)

        # Reached only when this is the failover target AND we are in FALLBACK.
        # ここに来るのは is_failover_target かつ tracker.mode が FALLBACK の場合のみ。
        requested_model: str | None = None
        try:
            requested_model = json.loads(body).get("model")
        except (json.JSONDecodeError, AttributeError):
            pass
        target_model = settings.model_mapping.resolve(requested_model) if requested_model else None
        try:
            response = await backends.forward_to_ollama(request, body, target_model)
        finally:
            # Ollamaが続けて失敗しているなら、Anthropicへ戻すかをトラッカーが判断する。
            # 例外で抜ける場合も通したいので finally に置く。
            # Let the tracker decide whether to go back to Anthropic when Ollama
            # keeps failing. In `finally` so it also runs when this raises.
            failures = backends.ollama_stats.consecutive_failures
            previous_mode = tracker.mode
            new_mode = tracker.note_ollama_failures(failures)
            if new_mode is not previous_mode:
                logger.warning(
                    "ollama failed %d times in a row; switching backend %s -> %s",
                    failures,
                    previous_mode.value,
                    new_mode.value,
                )
        return to_streaming_response(response)

    return app
