"""フォールバック中にAnthropicのquota回復を定期確認するバックグラウンドタスク。"""

from __future__ import annotations

import asyncio
import logging

from .backends import ProxyBackends
from .config import Settings
from .quota import BackendMode, QuotaTracker, parse_quota_headers

logger = logging.getLogger("claude_spillway.recovery")

# 転送を横流しする際に一緒に保存しておく、回復確認プローブへ再利用するヘッダー。
_CAPTURED_HEADER_NAMES = ("authorization", "x-api-key", "anthropic-version")


class RecoveryProbe:
    """フォールバックモード中、一定間隔でAnthropicへ軽量リクエストを送り回復を検知する。"""

    def __init__(self, settings: Settings, backends: ProxyBackends, tracker: QuotaTracker) -> None:
        self._settings = settings
        self._backends = backends
        self._tracker = tracker
        self._task: asyncio.Task[None] | None = None
        self._captured_headers: dict[str, str] = {}

    def capture_auth_headers(self, headers: dict[str, str]) -> None:
        """通常モードでの転送時に、認証ヘッダーを控えておく(プローブ送信用)。"""
        for name in _CAPTURED_HEADER_NAMES:
            if name in headers:
                self._captured_headers[name] = headers[name]

    def start(self) -> None:
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
                if self._tracker.mode is not BackendMode.FALLBACK:
                    return
                try:
                    await self._probe_once()
                except Exception:
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
