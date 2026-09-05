"""Background task that polls Anthropic for quota recovery while in fallback.

フォールバック中にAnthropicのquota回復を定期確認するバックグラウンドタスク。
"""

from __future__ import annotations

import asyncio
import logging

from .backends import ProxyBackends
from .config import Settings
from .quota import BackendMode, QuotaTracker, parse_quota_headers

logger = logging.getLogger("claude_spillway.recovery")

# Headers stashed away while relaying, to be reused by the recovery probe.
# 転送を横流しする際に一緒に保存しておく、回復確認プローブへ再利用するヘッダー。
_CAPTURED_HEADER_NAMES = ("authorization", "x-api-key", "anthropic-version")


class RecoveryProbe:
    """While in fallback, ping Anthropic periodically to detect recovery.

    フォールバックモード中、一定間隔でAnthropicへ軽量リクエストを送り回復を検知する。
    """

    def __init__(self, settings: Settings, backends: ProxyBackends, tracker: QuotaTracker) -> None:
        self._settings = settings
        self._backends = backends
        self._tracker = tracker
        self._task: asyncio.Task[None] | None = None
        self._captured_headers: dict[str, str] = {}

    def capture_auth_headers(self, headers: dict[str, str]) -> None:
        """Stash the auth headers seen while relaying normally (for probing).

        通常モードでの転送時に、認証ヘッダーを控えておく(プローブ送信用)。
        """
        for name in _CAPTURED_HEADER_NAMES:
            if name in headers:
                self._captured_headers[name] = headers[name]

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
            while self._tracker.mode is BackendMode.FALLBACK:
                await asyncio.sleep(interval)
                # The mode may have changed while we slept; re-check before probing.
                # 待機中にモードが変わっている可能性があるため、送信前に再確認する。
                if self._tracker.mode is not BackendMode.FALLBACK:
                    return
                try:
                    await self._probe_once()
                except Exception:
                    # A failed probe must not kill the loop; try again next tick.
                    # プローブの失敗でループを止めないよう、次回に持ち越す。
                    logger.exception("recovery probe failed")
        except asyncio.CancelledError:
            pass

    async def _probe_once(self) -> None:
        if not self._captured_headers:
            logger.debug("no captured Anthropic credentials yet; skipping recovery probe")
            return
        payload = {
            "model": self._settings.quota.probe_model,
            "max_tokens": self._settings.quota.probe_max_tokens,
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {**self._captured_headers, "content-type": "application/json"}
        response = await self._backends.probe_anthropic(headers, payload)
        snapshot = parse_quota_headers(response.headers)
        previous_mode = self._tracker.mode
        new_mode = self._tracker.observe(snapshot)
        ratio = snapshot.remaining_ratio()
        logger.info(
            "recovery probe: remaining=%s",
            f"{ratio * 100:.1f}%" if ratio is not None else "unknown",
        )
        if new_mode is not previous_mode:
            logger.warning(
                "quota recovered; switching backend %s -> %s", previous_mode.value, new_mode.value
            )
