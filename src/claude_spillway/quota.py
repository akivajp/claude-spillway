"""Anthropicのレート制限ヘッダーを解析し、バックエンド切替の要否を判断するモジュール。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import httpx


class BackendMode(str, Enum):
    """現在リクエストを転送しているバックエンドの種類。"""

    ANTHROPIC = "anthropic"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class QuotaSnapshot:
    """Anthropicのレスポンスヘッダーから読み取った、ある時点での利用状況。"""

    utilization_5h: float | None
    utilization_7d: float | None
    requests_remaining_ratio: float | None
    tokens_remaining_ratio: float | None
    observed_at: float

    def remaining_ratio(self) -> float | None:
        """既知の指標のうち最も逼迫している(残量が少ない)値を返す。

        5時間窓・週次窓・リクエスト数・トークン数のいずれか1つでも枯渇に
        近づけば早めにフェイルオーバーしたいため、最小値を採用する。
        """
        ratios: list[float] = []
        if self.utilization_5h is not None:
            ratios.append(1.0 - self.utilization_5h)
        if self.utilization_7d is not None:
            ratios.append(1.0 - self.utilization_7d)
        if self.requests_remaining_ratio is not None:
            ratios.append(self.requests_remaining_ratio)
        if self.tokens_remaining_ratio is not None:
            ratios.append(self.tokens_remaining_ratio)
        if not ratios:
            return None
        return min(ratios)


def _parse_float(headers: httpx.Headers, name: str) -> float | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_remaining_ratio(
    headers: httpx.Headers, remaining_name: str, limit_name: str
) -> float | None:
    remaining = _parse_float(headers, remaining_name)
    limit = _parse_float(headers, limit_name)
    if remaining is None or limit is None or limit <= 0:
        return None
    return remaining / limit


def parse_quota_headers(headers: httpx.Headers) -> QuotaSnapshot:
    """Anthropic APIのレスポンスヘッダーから :class:`QuotaSnapshot` を組み立てる。

    サブスクリプション(Pro/Max/Team)経由のClaude Codeでは
    ``anthropic-ratelimit-unified-{5h,7d}-utilization`` が、
    APIキー課金の場合は ``anthropic-ratelimit-{requests,tokens}-*`` が
    それぞれ付与される。両方に対応し、存在するものだけを読み取る。
    """
    return QuotaSnapshot(
        utilization_5h=_parse_float(headers, "anthropic-ratelimit-unified-5h-utilization"),
        utilization_7d=_parse_float(headers, "anthropic-ratelimit-unified-7d-utilization"),
        requests_remaining_ratio=_parse_remaining_ratio(
            headers,
            "anthropic-ratelimit-requests-remaining",
            "anthropic-ratelimit-requests-limit",
        ),
        tokens_remaining_ratio=_parse_remaining_ratio(
            headers,
            "anthropic-ratelimit-tokens-remaining",
            "anthropic-ratelimit-tokens-limit",
        ),
        observed_at=time.time(),
    )


class QuotaTracker:
    """現在のバックエンドモードを保持し、閾値に基づいて切替を判断する状態機械。

    fallback_threshold_pct と recovery_threshold_pct の間にヒステリシスを
    設けることで、残量が閾値付近で微振動して頻繁に切り替わる
    (flapping)のを防ぐ。
    """

    def __init__(self, fallback_threshold_pct: float, recovery_threshold_pct: float) -> None:
        if recovery_threshold_pct <= fallback_threshold_pct:
            raise ValueError(
                "recovery_threshold_pct must be greater than fallback_threshold_pct "
                f"(got fallback={fallback_threshold_pct}, recovery={recovery_threshold_pct})"
            )
        self._fallback_threshold = fallback_threshold_pct / 100.0
        self._recovery_threshold = recovery_threshold_pct / 100.0
        self.mode: BackendMode = BackendMode.ANTHROPIC
        self.last_snapshot: QuotaSnapshot | None = None
        self.last_switch_at: float = time.time()

    def observe(self, snapshot: QuotaSnapshot) -> BackendMode:
        """新しいスナップショットを取り込み、必要なら切り替えて結果のモードを返す。"""
        self.last_snapshot = snapshot
        ratio = snapshot.remaining_ratio()
        if ratio is None:
            return self.mode

        if self.mode is BackendMode.ANTHROPIC and ratio < self._fallback_threshold:
            self._switch_to(BackendMode.FALLBACK)
        elif self.mode is BackendMode.FALLBACK and ratio >= self._recovery_threshold:
            self._switch_to(BackendMode.ANTHROPIC)
        return self.mode

    def _switch_to(self, mode: BackendMode) -> None:
        self.mode = mode
        self.last_switch_at = time.time()
