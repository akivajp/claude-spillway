"""Background task that keeps the quota reading fresh.

Its original job was detecting recovery while in fallback, and it still does
that. It now also refreshes quota in normal mode, because the OAuth usage
endpoint reports it without consuming any: the monitor stays live even when no
traffic is flowing.

quota観測値を最新に保つバックグラウンドタスク。
元々の役割はフォールバック中の回復検知であり、それは変わらない。加えて、
OAuthの使用量エンドポイントはquotaを消費せずに残量を取得できるため、通常モード
中もquotaを更新する。これによりトラフィックが流れていない間もmonitorの表示が
最新に保たれる。
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .backends import ProxyBackends
from .config import Settings, load_ollama_session
from .models import DirectModelRegistry, parse_ollama_models
from .quota import (
    BackendMode,
    OllamaResetEstimator,
    OllamaSnapshot,
    QuotaSnapshot,
    QuotaTracker,
    parse_ollama_usage,
    parse_ollama_usage_page,
    parse_quota_headers,
    parse_usage_payload,
)

logger = logging.getLogger("claude_spillway.recovery")

# Headers stashed away while relaying, to be reused by the recovery probe.
# 転送を横流しする際に一緒に保存しておく、回復確認プローブへ再利用するヘッダー。
_CAPTURED_HEADER_NAMES = ("authorization", "x-api-key", "anthropic-version")

#: Reasons Ollama's utilization may be unreadable, as reported in the status.
#: ステータスで報告する、Ollamaの使用率を読めない理由。
#:   session_expired     - the settings page bounced us to sign-in (cookie stale)
#:                         設定ページがサインインへ転送した(Cookie失効)
#:   page_not_understood - the settings page no longer has the meters we read
#:                         設定ページに読み取り対象のメーターが無い(形式変更)
#:   api_no_utilization  - no cookie, and the API-key endpoint has no utilization
#:                         Cookie未設定で、APIキーのエンドポイントは使用率を返さない
OLLAMA_PROBLEM_CODES = ("session_expired", "page_not_understood", "api_no_utilization")


class RecoveryProbe:
    """Poll Anthropic for the current quota, and switch back once it recovers.

    フォールバックからの回復を検知するため、Anthropicのquotaを定期的に確認する。
    """

    def __init__(
        self,
        settings: Settings,
        backends: ProxyBackends,
        tracker: QuotaTracker,
        direct_models: DirectModelRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._backends = backends
        self._tracker = tracker
        #: 既定値を持たせているのは、モデル一覧を使わない呼び出し(テスト等)でも
        #: そのまま構築できるようにするため。
        #: Defaulted so callers that do not care about the model list (tests,
        #: for one) can still construct this the way they always did.
        self._direct_models = direct_models or DirectModelRegistry(settings.direct_models)
        self._models_refreshed_at: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._captured_headers: dict[str, str] = {}
        # Set once the usage endpoint answers in a way that will not change on a
        # retry (no OAuth token, endpoint gone, beta withdrawn). Prevents
        # hammering a URL that is never going to work for this account.
        # 使用量エンドポイントが「再試行しても変わらない」形で失敗した場合に立てる
        # (OAuthトークンが無い、エンドポイントが消えた、beta提供終了など)。
        # このアカウントでは成功し得ないURLを叩き続けるのを防ぐ。
        self._usage_unavailable = False
        # 同上、Ollama側の使用量エンドポイントについて。
        # The same, for Ollama's usage endpoint.
        self._ollama_usage_unavailable = False
        # Ollamaはリセット時刻を返さないため、利用率の上昇から上限値を推定する。
        # Ollama reports no reset times, so bound them from utilization rises.
        self._ollama_reset_estimator = OllamaResetEstimator()
        # 設定ページを最後に読みに行った時刻。ページは毎ティック読まず間隔を空ける。
        # When the settings page was last fetched; it is not read on every tick.
        self._ollama_page_fetched_at: float | None = None
        # 最後に設定ページへ送ったCookie。差し替えを検知して即座に読み直すために使う。
        # The cookie last sent to the settings page, to notice a replacement.
        self._ollama_page_cookie: str | None = None
        #: Expiry (epoch seconds) of the ollama.com session in use, when known.
        #: Only a session saved by ``ollama-login`` carries one.
        #: 使用中のollama.comセッションの有効期限(エポック秒)。分かる場合のみ。
        #: ``ollama-login`` で保存したセッションだけが期限を持つ。
        self.ollama_session_expires: float | None = None
        #: Why Ollama's utilization cannot be read right now, or ``None`` if it
        #: can. One of :data:`OLLAMA_PROBLEM_CODES`; shown by the monitor views.
        #: Ollamaの使用率を現在読めない理由。読めていれば ``None``。
        #: 値は :data:`OLLAMA_PROBLEM_CODES` のいずれかで、監視画面に表示される。
        self.ollama_usage_problem: str | None = None

    def capture_auth_headers(self, headers: dict[str, str]) -> None:
        """Stash the auth headers seen while relaying normally (for probing).

        The proxy holds no Anthropic credentials of its own, so this is the only
        way it ever gets one. Polling can only start once a real request has
        passed through.

        通常モードでの転送時に、認証ヘッダーを控えておく(プローブ送信用)。
        このプロキシはAnthropicの認証情報を自前で持たないため、これが唯一の入手
        経路であり、実リクエストが1度通るまでポーリングは開始できない。
        """
        for name in _CAPTURED_HEADER_NAMES:
            if name in headers:
                self._captured_headers[name] = headers[name]
        # A credential just arrived: start polling even in normal mode, so the
        # monitor has fresh numbers without waiting for the next request.
        # 資格情報が手に入ったので、通常モードでもポーリングを開始する。
        # 次のリクエストを待たずにmonitorへ最新の数値を出せるようにするため。
        if self._captured_headers:
            self.start()

    def start(self) -> None:
        # Idempotent: a probe loop is only started if none is running.
        # 冪等: 既に走っているループがある場合は新たに起動しない。
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        interval = self._settings.quota.probe_interval_seconds
        try:
            # Ollamaの残量だけは待たずに読む。ループが始まるのは最初のリクエストを
            # 中継した直後だが、ここで待ってしまうと ollama_snapshot が None の
            # まま interval 秒が過ぎる。その間は「枯渇したOllamaへ送らない」
            # ガードが判定材料を持てず、起動直後だけ無防備になる。
            # 読み取りは無料かつ資格情報も不要なので、待つ理由がない。
            # Read Ollama's headroom without waiting. The loop starts right after
            # the first relayed request, and sleeping first would leave
            # ollama_snapshot None for a whole interval - exactly when the guard
            # against routing into an exhausted Ollama has nothing to judge on.
            # The read is free and needs no captured credential, so nothing is
            # gained by deferring it.
            try:
                await self._refresh_ollama_usage()
            except Exception:
                logger.exception("initial ollama usage read failed")
            # モデル一覧も起動時点で読む。これが無いと、最初のリフレッシュまでの
            # 間にOllama側のモデルを指定されてもAnthropicへ中継されてしまい、
            # まさに直結が防ごうとしている404が返る。
            # Read the model list at startup too: without it, an Ollama-side
            # model named before the first refresh would be relayed to Anthropic
            # and produce exactly the 404 direct routing exists to prevent.
            try:
                await self._refresh_ollama_models()
            except Exception:
                logger.exception("initial ollama model listing failed")

            while True:
                await asyncio.sleep(interval)
                # モデル一覧の更新は認証ヘッダーを必要としないため、Anthropicの
                # quota確認とは独立に、捕捉の有無に関わらず行う。
                # Refreshing the model list needs no captured credential, so it
                # runs independently of the Anthropic quota check.
                try:
                    await self._refresh_ollama_models()
                except Exception:
                    logger.exception("ollama model listing failed")
                if not self._captured_headers:
                    continue
                try:
                    await self._probe_once()
                except Exception:
                    # A failed probe must not kill the loop; try again next tick.
                    # プローブの失敗でループを止めないよう、次回に持ち越す。
                    logger.exception("quota probe failed")
        except asyncio.CancelledError:
            pass

    async def _probe_once(self) -> None:
        previous_mode = self._tracker.mode
        # Ollama側も毎回更新する。読み取りはリクエスト数に計上されないため無料。
        # Refresh Ollama too; reading its usage is not counted as a request.
        await self._refresh_ollama_usage()
        snapshot = await self._read_usage_endpoint()
        if snapshot is None and previous_mode is BackendMode.FALLBACK:
            # No free reading available, and we are in fallback with no relayed
            # traffic to learn from: spend a minimal request to detect recovery.
            # 無料で読める手段が無く、かつフォールバック中で中継トラフィックからも
            # 学習できないため、回復検知のために最小のリクエストを1回だけ使う。
            snapshot = await self._read_messages_probe()
        if snapshot is None:
            return

        new_mode = self._tracker.observe(snapshot)
        ratio = snapshot.remaining_ratio()
        logger.info(
            "quota probe (%s): remaining=%s",
            snapshot.source,
            f"{ratio * 100:.1f}%" if ratio is not None else "unknown",
        )
        if new_mode is not previous_mode:
            logger.warning(
                "quota recovered; switching backend %s -> %s", previous_mode.value, new_mode.value
            )

    async def _refresh_ollama_models(self) -> None:
        """Refresh the list of models Ollama Cloud serves, at most once per interval.

        Failures are quiet and leave the previous list in place: the proxy then
        keeps routing the names it already knows, which is strictly better than
        relaying them to an Anthropic that will 404 on every one.

        Ollama Cloudが提供するモデルの一覧を、設定された間隔ごとに取り直す。
        失敗しても静かに諦め、直前の一覧をそのまま残す。既に把握している名前は
        引き続き直結できるため、全件404になるAnthropicへ中継し始めるよりよい。
        """
        if not self._direct_models.discovery_enabled or not self._settings.ollama.api_key:
            return
        now = time.monotonic()
        if (
            self._models_refreshed_at is not None
            and now - self._models_refreshed_at < self._direct_models.refresh_interval_seconds
        ):
            return
        try:
            response = await self._backends.fetch_ollama_models()
        except httpx.HTTPError as exc:
            logger.debug("ollama model listing request failed: %s", exc)
            return
        # 成功・失敗によらず時刻を進める。失敗を毎ティック叩き直さないため。
        # Advance the clock either way, so a failure is not retried every tick.
        self._models_refreshed_at = now
        if response.status_code != 200:
            logger.info("ollama model listing unavailable (HTTP %s)", response.status_code)
            return
        try:
            names = parse_ollama_models(response.json())
        except ValueError as exc:
            logger.info("ollama model listing not understood (%s); ignoring", exc)
            return
        if not names:
            logger.info("ollama model listing returned no usable names; keeping previous list")
            return
        previous = set(self._direct_models.discovered)
        self._direct_models.update(names)
        if previous != set(names):
            logger.info("ollama cloud models: %d known (%s)", len(names), ", ".join(names))

    async def _refresh_ollama_usage(self) -> None:
        """Read Ollama's usage and hand it to the tracker.

        With a session cookie the account's settings page is read (it alone
        carries utilization and reset times since 2026-10); without one, the
        API-key usage endpoint is tried. Failures are quiet: without this
        reading the proxy simply routes the way it always did, so it is never
        worth interrupting the loop over.

        Ollamaの使用状況を読み、トラッカーへ渡す。
        セッションCookieがあればアカウントの設定ページを読む(2026-10以降、使用率と
        リセット時刻を載せているのはここだけ)。無ければAPIキーの使用量エンドポイントを
        試す。失敗しても静かに諦める。この値が無くても従来どおりのルーティングに
        なるだけなので、ループを止めてまで扱う価値はない。
        """
        # 保存ファイルは毎回読み直す。ollama-login で更新されたCookieを
        # 再起動なしで拾うため(読み取りは小さなJSON1つで安い)。
        # Re-read the saved session every time, so a cookie refreshed by
        # ollama-login is picked up without a restart (one small JSON file).
        session = load_ollama_session(self._settings.ollama)
        self.ollama_session_expires = session.expires if session else None
        if session is not None:
            snapshot = await self._read_ollama_usage_page(session.cookie)
        else:
            snapshot = await self._read_ollama_usage_api()
        if snapshot is None or snapshot.remaining_ratio() is None:
            return
        if snapshot.session_reset is None or snapshot.weekly_reset is None:
            # 実測のリセット時刻が無い場合に限り、利用率の動きから上限値を推定する。
            # 表示専用であり、ルーティング判断は実測値のみで行われる。
            # Only without measured resets, bound them from utilization movement.
            # Display-only: routing decisions still run on the measured values alone.
            snapshot = self._ollama_reset_estimator.update(snapshot)
        self._tracker.observe_ollama(snapshot)

    async def _read_ollama_usage_page(self, cookie: str) -> OllamaSnapshot | None:
        """Read utilization and reset times from the ollama.com settings page.

        Fetched at most once per ``usage_page_interval_seconds``, since this is
        a page meant for people - except right after the cookie changes. An
        expired session is reported once and then left alone until a different
        cookie shows up, since retrying the same one is futile.

        ollama.comの設定ページから使用率とリセット時刻を読む。
        人が見るためのページなので、``usage_page_interval_seconds`` に1回までしか
        読まない(Cookieが差し替わった直後だけは即座に読む)。セッション失効は
        1回だけ報告し、別のCookieが現れるまでは読みに行かない。同じCookieで
        再試行しても無駄なため。
        """
        changed = cookie != self._ollama_page_cookie
        if self.ollama_usage_problem == "session_expired" and not changed:
            return None
        now = time.time()
        interval = self._settings.ollama.usage_page_interval_seconds
        recent = self._ollama_page_fetched_at is not None and now - self._ollama_page_fetched_at < interval
        if recent and not changed:
            return None
        self._ollama_page_fetched_at = now
        self._ollama_page_cookie = cookie
        try:
            response = await self._backends.fetch_ollama_usage_page(cookie)
        except httpx.HTTPError as exc:
            # 一時的な障害とみなし、次の間隔で再試行する。
            # Transient: try again after the next interval.
            logger.debug("ollama usage page request failed: %s", exc)
            return None
        if response.is_redirect or response.status_code in (401, 403):
            # サインインへの転送 = セッションCookieが失効している。
            # A bounce to sign-in means the session cookie has gone stale.
            self.ollama_usage_problem = "session_expired"
            logger.warning(
                "ollama session cookie rejected (HTTP %s); run `claude-spillway ollama-login` "
                "to sign in again",
                response.status_code,
            )
            return None
        if response.status_code != 200:
            logger.debug("ollama usage page returned HTTP %s", response.status_code)
            return None
        snapshot = parse_ollama_usage_page(response.text)
        if snapshot is None:
            # ページの形が変わった可能性がある。直る可能性もあるため止めはしないが、
            # 毎回ログを出さないよう、状態が変わったときだけ報告する。
            # The page may have been redesigned. Keep trying in case it is
            # transient, but only log when the state changes.
            if self.ollama_usage_problem != "page_not_understood":
                logger.warning("ollama usage page has no usage meters we can read; ignoring")
            self.ollama_usage_problem = "page_not_understood"
            return None
        self.ollama_usage_problem = None
        return snapshot

    async def _read_ollama_usage_api(self) -> OllamaSnapshot | None:
        """Read Ollama's API-key usage endpoint (no utilization since 2026-10).

        APIキーで読めるOllamaの使用量エンドポイントを読む(2026-10以降は使用率を返さない)。
        """
        if self._ollama_usage_unavailable or not self._settings.ollama.api_key:
            return None
        try:
            response = await self._backends.fetch_ollama_usage()
        except httpx.HTTPError as exc:
            logger.debug("ollama usage request failed: %s", exc)
            return None
        if response.status_code != 200:
            if 400 <= response.status_code < 500 and response.status_code != 429:
                self._ollama_usage_unavailable = True
                logger.info("ollama usage endpoint unavailable (HTTP %s)", response.status_code)
            return None
        try:
            snapshot = parse_ollama_usage(response.json())
        except (ValueError, AttributeError, TypeError) as exc:
            logger.info("ollama usage payload not understood (%s); ignoring", exc)
            self._ollama_usage_unavailable = True
            return None
        if snapshot.remaining_ratio() is None:
            # 2026-10以降の形式(リクエスト数のみ)。再試行しても変わらないため以後試さず、
            # 黙って捨てずに一度だけ理由を残す。
            # The post-2026-10 shape (request counts only). Retrying will not
            # change it, so stop polling and say why once instead of silently.
            self._ollama_usage_unavailable = True
            self.ollama_usage_problem = "api_no_utilization"
            logger.warning(
                "ollama usage endpoint reports no utilization; run `claude-spillway ollama-login` "
                "to read Ollama's quota from the settings page"
            )
            return None
        return snapshot

    async def _read_usage_endpoint(self) -> QuotaSnapshot | None:
        """Read quota from the OAuth usage endpoint. Returns ``None`` if unusable.

        OAuth使用量エンドポイントからquotaを読む。使えない場合は ``None``。
        """
        if not self._settings.quota.use_usage_endpoint or self._usage_unavailable:
            return None
        authorization = self._captured_headers.get("authorization")
        if not authorization:
            # API-key auth: this endpoint is OAuth-only, so never try it.
            # APIキー認証の場合、このエンドポイントはOAuth専用なので試さない。
            self._usage_unavailable = True
            logger.debug("no OAuth bearer token captured; usage endpoint disabled")
            return None

        try:
            response = await self._backends.fetch_oauth_usage(authorization)
        except httpx.HTTPError as exc:
            # Transient: keep the endpoint enabled and retry next tick.
            # 一時的な障害とみなし、無効化せず次回に再試行する。
            logger.debug("usage endpoint request failed: %s", exc)
            return None

        if response.status_code != 200:
            # 4xx other than 429 will not fix itself: stop trying. 429 and 5xx
            # are transient, so leave the endpoint enabled.
            # 429以外の4xxは再試行しても直らないため以後試さない。429と5xxは
            # 一時的なものとみなして有効なままにする。
            if 400 <= response.status_code < 500 and response.status_code != 429:
                self._usage_unavailable = True
                logger.info(
                    "usage endpoint unavailable (HTTP %s); falling back to relayed headers",
                    response.status_code,
                )
            else:
                logger.debug("usage endpoint returned HTTP %s", response.status_code)
            return None

        try:
            snapshot = parse_usage_payload(response.json())
        except (ValueError, AttributeError, TypeError) as exc:
            # The payload is not part of the published API; a shape change is
            # "no data", not a crash.
            # このレスポンスは公開APIではないため、形が変わっても落とさずに
            # 「データなし」として扱う。
            logger.info("usage endpoint payload not understood (%s); ignoring", exc)
            self._usage_unavailable = True
            return None

        if snapshot.remaining_ratio() is None:
            logger.debug("usage endpoint returned no usable window")
            return None
        return snapshot

    async def _read_messages_probe(self) -> QuotaSnapshot | None:
        """Send a minimal request and read quota off its response headers.

        This does consume a little quota, which is why it is the fallback.

        最小限のリクエストを送り、そのレスポンスヘッダーからquotaを読む。
        わずかにquotaを消費するため、あくまでフォールバック手段。
        """
        payload = {
            "model": self._settings.quota.probe_model,
            "max_tokens": self._settings.quota.probe_max_tokens,
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {**self._captured_headers, "content-type": "application/json"}
        response = await self._backends.probe_anthropic(headers, payload)
        return parse_quota_headers(response.headers)
