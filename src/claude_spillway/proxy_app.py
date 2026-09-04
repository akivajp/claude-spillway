"""Claude Codeから見て `ANTHROPIC_BASE_URL` の向き先となるFastAPIアプリ。"""

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
from .quota import BackendMode, QuotaTracker
from .recovery import RecoveryProbe

logger = logging.getLogger("claude_spillway.proxy")

#: フェイルオーバーの対象とする唯一のエンドポイント。
#: count_tokens やモデル一覧取得等はOllamaの互換シムが不安定なため
#: (ollama/ollama#13949) 常にAnthropicへ流す。
_FAILOVER_PATH = "v1/messages"

_STARTED_AT = time.time()


def create_app(settings: Settings, backends: ProxyBackends | None = None) -> FastAPI:
    """アプリを構築する。``backends`` はテストからモック注入するために公開している。"""
    backends = backends or ProxyBackends(settings)
    tracker = QuotaTracker(
        fallback_threshold_pct=settings.quota.fallback_threshold_pct,
        recovery_threshold_pct=settings.quota.recovery_threshold_pct,
    )
    recovery_probe = RecoveryProbe(settings, backends, tracker)

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await recovery_probe.stop()
        await backends.aclose()

    app = FastAPI(title="claude-spillway", lifespan=_lifespan)
    app.state.backends = backends
    app.state.tracker = tracker
    app.state.recovery_probe = recovery_probe
    app.state.settings = settings

    @app.get("/_spillway/status")
    async def status() -> JSONResponse:
        """現在のバックエンドモードやquota観測値を返す監視用エンドポイント(TUIから利用)。"""
        snapshot = tracker.last_snapshot
        stats = backends.ollama_stats
        return JSONResponse(
            {
                "mode": tracker.mode.value,
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
                },
                "ollama": {
                    # Ollama Cloudには公式のquota取得APIが無いため(2026-09時点)、
                    # このプロキシが中継したリクエストの自己計測値のみを提供する。
                    "requests_sent": stats.requests_sent,
                    "requests_failed": stats.requests_failed,
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
            if is_failover_target and snapshot is not None:
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
            return to_streaming_response(response)

        # ここに来るのは is_failover_target かつ tracker.mode が FALLBACK の場合のみ。
        requested_model: str | None = None
        try:
            requested_model = json.loads(body).get("model")
        except (json.JSONDecodeError, AttributeError):
            pass
        target_model = settings.model_mapping.resolve(requested_model) if requested_model else None
        response = await backends.forward_to_ollama(request, body, target_model)
        return to_streaming_response(response)

    return app
