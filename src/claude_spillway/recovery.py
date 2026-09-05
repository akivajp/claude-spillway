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

import httpx

from .backends import ProxyBackends
from .config import Settings
from .quota import (
    BackendMode,
    OllamaResetEstimator,
    QuotaSnapshot,
    QuotaTracker,
    parse_ollama_usage,
    parse_quota_headers,
    parse_usage_payload,
)

logger = logging.getLogger("claude_spillway.recovery")

# Headers stashed away while relaying, to be reused by the recovery probe.
# 転送を横流しする際に一緒に保存しておく、回復確認プローブへ再利用するヘッダー。
_CAPTURED_HEADER_NAMES = ("authorization", "x-api-key", "anthropic-version")


class RecoveryProbe:
    """Poll Anthropic for the current quota, and switch back once it recovers.

    フォールバックからの回復を検知するため、Anthropicのquotaを定期的に確認する。
    """

    def __init__(self, settings: Settings, backends: ProxyBackends, tracker: QuotaTracker) -> None:
        self._settings = settings
        self._backends = backends
        self._tracker = tracker
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
            while True:
                await asyncio.sleep(interval)
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

    async def _refresh_ollama_usage(self) -> None:
        """Read Ollama's own usage endpoint and hand it to the tracker.

        Failures are quiet: without this reading the proxy simply routes the way
        it always did, so it is never worth interrupting the loop over.

        Ollama自身の使用量エンドポイントを読み、トラッカーへ渡す。
        失敗しても静かに諦める。この値が無くても従来どおりのルーティングになる
        だけなので、ループを止めてまで扱う価値はない。
        """
        if self._ollama_usage_unavailable or not self._settings.ollama.api_key:
            return
        try:
            response = await self._backends.fetch_ollama_usage()
        except httpx.HTTPError as exc:
            logger.debug("ollama usage request failed: %s", exc)
            return
        if response.status_code != 200:
            if 400 <= response.status_code < 500 and response.status_code != 429:
                self._ollama_usage_unavailable = True
                logger.info("ollama usage endpoint unavailable (HTTP %s)", response.status_code)
            return
        try:
            snapshot = parse_ollama_usage(response.json())
        except (ValueError, AttributeError, TypeError) as exc:
            logger.info("ollama usage payload not understood (%s); ignoring", exc)
            self._ollama_usage_unavailable = True
            return
        if snapshot.remaining_ratio() is None:
            return
        # リセット時刻を返さないOllamaのために、利用率の動きから上限値を推定する。
        # 表示専用であり、ルーティング判断は実測値のみで行われる。
        # Ollama reports no reset times, so bound them from utilization movement.
        # Display-only: routing decisions still run on the measured values alone.
        snapshot = self._ollama_reset_estimator.update(snapshot)
        self._tracker.observe_ollama(snapshot)

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
