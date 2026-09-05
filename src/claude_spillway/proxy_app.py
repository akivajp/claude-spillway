"""FastAPI app that Claude Code points ``ANTHROPIC_BASE_URL`` at.

Claude Codeから見て ``ANTHROPIC_BASE_URL`` の向き先となるFastAPIアプリ。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .backends import ProxyBackends, to_streaming_response
from .config import Settings
from .i18n import get_language, t
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

#: Labels the dashboard needs, mapped onto the keys its JavaScript reads.
#: Most are shared with the TUI so the two views cannot drift in wording.
#:
#: ダッシュボードが必要とする文言と、JavaScript側のキーの対応表。
#: 大半はTUIと共通のキーを指しており、二つの画面で文言がずれないようにしている。
_DASHBOARD_LABELS: dict[str, str] = {
    "badge_anthropic": "dashboard.badge.anthropic",
    "badge_fallback": "dashboard.badge.fallback",
    "badge_offline": "dashboard.badge.offline",
    "refresh": "dashboard.refresh",
    "paused": "dashboard.paused",
    "unreachable": "dashboard.unreachable",
    "failed_suffix": "dashboard.failed_suffix",
    "policy": "dashboard.policy",
    "uptime": "dashboard.uptime",
    "mark_fallback": "dashboard.mark.fallback",
    "mark_recovery": "dashboard.mark.recovery",
    "ollama_sub": "dashboard.ollama.sub",
    "estimate_note": "dashboard.ollama.estimate_note",
    "waiting": "monitor.waiting.body",
    "thresholds": "monitor.footer.thresholds",
    "reset_in": "monitor.reset.in",
    "window_5h": "monitor.row.window_5h",
    "window_7d": "monitor.row.window_7d",
    "session_window": "monitor.row.session_window",
    "weekly_window": "monitor.row.weekly_window",
    "requests": "monitor.row.requests",
    "tokens": "monitor.row.tokens",
    "observed_at": "monitor.col.observed_at",
    "top_models": "monitor.row.top_models",
    "relayed": "monitor.row.relayed",
    "consecutive_failures": "monitor.row.consecutive_failures",
    "last_status": "monitor.row.last_status",
    "last_error": "monitor.row.last_error",
    "source_headers": "monitor.source.headers",
    "source_usage_api": "monitor.source.usage_api",
}

#: Rendered pages keyed by language. The page is static once built, and it is
#: served on every browser refresh, so building it once is worth the dict.
#: 言語ごとの描画済みページ。組み立て後は不変で、ブラウザの更新のたびに
#: 返すものなので、一度だけ組み立てて使い回す。
_dashboard_cache: dict[str, str] = {}


def render_dashboard() -> str:
    """Return the dashboard page with the current language's labels baked in.

    Placeholders are substituted rather than templated so that the ``.html``
    file stays valid, editable HTML on its own.

    現在の言語の文言を埋め込んだダッシュボードのHTMLを返す。
    テンプレートエンジンを使わずプレースホルダ置換にしているのは、``.html``
    ファイル単体でも正しいHTMLとして編集・確認できるようにするため。
    """
    language = get_language()
    cached = _dashboard_cache.get(language)
    if cached is not None:
        return cached

    # ``{...}`` を含むCSS/JSと衝突しないよう、str.format ではなく置換を使う。
    # Plain replacement, not str.format: the CSS and JS are full of braces.
    source = resources.files(__package__).joinpath("dashboard.html").read_text(encoding="utf-8")
    labels = {name: t(key) for name, key in _DASHBOARD_LABELS.items()}
    page = source.replace("__CS_LANG__", language).replace(
        # ``</script>`` がラベル中に現れてもHTMLを壊さないよう、``/`` を退避する。
        # Escape ``/`` so a label containing ``</script>`` cannot break out.
        "__CS_LABELS__",
        json.dumps(labels, ensure_ascii=False).replace("</", "<\\/"),
    )
    _dashboard_cache[language] = page
    return page


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

    # ``/_spillway`` と ``/_spillway/`` の両方を明示的に登録する。末尾スラッシュの
    # 自動リダイレクトは「どのルートにも一致しなかった場合」にしか働かず、ここでは
    # 下の総当たりルートが先に拾ってAnthropicへ中継されてしまうため。
    # Both spellings are registered explicitly: FastAPI's trailing-slash redirect
    # only fires when nothing matched, and here the catch-all below would match
    # first and relay the request to Anthropic.
    @app.get("/_spillway", include_in_schema=False)
    @app.get("/_spillway/", include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        """Serve the browser dashboard that renders the status endpoint.

        ステータスAPIを可視化するブラウザ用ダッシュボードを返す。
        """
        # 更新のたびに取り直させる。バージョンアップ後に古いページが残ると、
        # 表示だけ古いという分かりにくい状態になるため。
        # Always refetch: a stale page after an upgrade is a confusing failure.
        return HTMLResponse(render_dashboard(), headers={"Cache-Control": "no-store"})

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
