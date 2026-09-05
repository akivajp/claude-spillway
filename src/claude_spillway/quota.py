"""Parse Anthropic's rate-limit headers and decide when to switch backends.

Anthropicのレート制限ヘッダーを解析し、バックエンド切替の要否を判断するモジュール。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx

#: Reading taken from the rate-limit headers of a relayed response.
#: 中継したレスポンスのレート制限ヘッダーから読み取った観測値。
SOURCE_HEADERS = "headers"

#: Reading taken from the OAuth usage endpoint, which consumes no quota.
#: quotaを消費しないOAuthの使用量エンドポイントから取得した観測値。
SOURCE_USAGE_API = "usage_api"


def parse_timestamp(raw: str | None) -> float | None:
    """Parse a reset timestamp into epoch seconds, or ``None`` if unusable.

    Accepts both epoch seconds and RFC 3339, because the documented
    ``anthropic-ratelimit-*-reset`` headers use RFC 3339 while other sources
    report plain epoch seconds; taking both costs one branch and avoids
    guessing wrong.

    リセット時刻をエポック秒として解釈する。解釈できなければ ``None``。
    公式ドキュメントに記載のある ``anthropic-ratelimit-*-reset`` はRFC 3339
    形式だが、エポック秒で返す経路もあるため両方を受け付ける(分岐1つで済み、
    形式を決め打ちして外すリスクを避けられる)。末尾の ``Z`` はPython 3.11の
    ``fromisoformat`` がそのまま解釈できる。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


class BackendMode(str, Enum):
    """Which backend requests are currently forwarded to.

    現在リクエストを転送しているバックエンドの種類。
    """

    ANTHROPIC = "anthropic"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class QuotaSnapshot:
    """Usage at a point in time, read from Anthropic's response headers.

    Anthropicのレスポンスヘッダーから読み取った、ある時点での利用状況。
    """

    utilization_5h: float | None
    utilization_7d: float | None
    requests_remaining_ratio: float | None
    tokens_remaining_ratio: float | None
    observed_at: float
    #: Epoch seconds at which each window is fully replenished, when known.
    #: 各ウィンドウが回復し切る時刻(エポック秒)。不明なら None。
    reset_5h: float | None = None
    reset_7d: float | None = None
    #: Where this reading came from, for display and debugging.
    #: この観測値の取得元(表示・デバッグ用)。
    source: str = SOURCE_HEADERS

    def remaining_ratio(self) -> float | None:
        """Return the tightest (smallest) remaining ratio among known signals.

        We want to fail over early if any one of the 5-hour window, weekly
        window, request count or token count is close to exhaustion, so the
        minimum is used.

        既知の指標のうち最も逼迫している(残量が少ない)値を返す。
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
    """Build a :class:`QuotaSnapshot` from Anthropic API response headers.

    Claude Code on a subscription (Pro/Max/Team) receives
    ``anthropic-ratelimit-unified-{5h,7d}-utilization``, while API-key billing
    receives ``anthropic-ratelimit-{requests,tokens}-*``. Both are supported;
    only the headers that are actually present get read.

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
        reset_5h=parse_timestamp(headers.get("anthropic-ratelimit-unified-5h-reset")),
        reset_7d=parse_timestamp(headers.get("anthropic-ratelimit-unified-7d-reset")),
        source=SOURCE_HEADERS,
    )


def _percent_to_ratio(value: Any) -> float | None:
    """Convert a 0-100 utilization percentage into a 0-1 ratio.

    The usage endpoint reports utilization as a percentage while the headers
    report it as a ratio. Mixing the two silently misreads quota by 100x, so
    the conversion is done once, here.

    0〜100のパーセント表記の使用率を0〜1の比率へ変換する。
    使用量エンドポイントはパーセント、ヘッダーは比率で返すため、混同すると
    100倍ずれた値を静かに読み違える。変換はここ1箇所に集約する。
    """
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value) / 100.0))


def parse_usage_payload(payload: dict[str, Any]) -> QuotaSnapshot:
    """Build a :class:`QuotaSnapshot` from the OAuth usage endpoint response.

    The endpoint is the one Claude Code itself reads for ``/usage``. It is not
    part of the published API, so treat a change of shape as "no data" rather
    than an error: every field is read defensively.

    OAuthの使用量エンドポイントのレスポンスから :class:`QuotaSnapshot` を作る。
    Claude Code自身が ``/usage`` で参照しているものと同じだが、公開APIでは
    ないため、形が変わった場合はエラーではなく「データなし」として扱えるよう
    全フィールドを防御的に読む。
    """
    five_hour = payload.get("five_hour") or {}
    seven_day = payload.get("seven_day") or {}
    return QuotaSnapshot(
        utilization_5h=_percent_to_ratio(five_hour.get("utilization")),
        utilization_7d=_percent_to_ratio(seven_day.get("utilization")),
        # 使用量エンドポイントはリクエスト数/トークン数の枠を返さない。
        # The usage endpoint reports no request/token allowances.
        requests_remaining_ratio=None,
        tokens_remaining_ratio=None,
        observed_at=time.time(),
        reset_5h=parse_timestamp(five_hour.get("resets_at")),
        reset_7d=parse_timestamp(seven_day.get("resets_at")),
        source=SOURCE_USAGE_API,
    )


class QuotaTracker:
    """State machine holding the current backend mode and threshold decisions.

    Keeping fallback_threshold_pct and recovery_threshold_pct apart creates
    hysteresis, which stops the mode from flapping while the remaining ratio
    hovers around a single threshold.

    現在のバックエンドモードを保持し、閾値に基づいて切替を判断する状態機械。
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
        """Take in a new snapshot, switch mode if needed, and return the mode.

        新しいスナップショットを取り込み、必要なら切り替えて結果のモードを返す。
        """
        self.last_snapshot = snapshot
        ratio = snapshot.remaining_ratio()
        if ratio is None:
            # No usable signal in this response; keep the current mode.
            # 判断材料が無いレスポンスなので、現在のモードを維持する。
            return self.mode

        if self.mode is BackendMode.ANTHROPIC and ratio < self._fallback_threshold:
            self._switch_to(BackendMode.FALLBACK)
        elif self.mode is BackendMode.FALLBACK and ratio >= self._recovery_threshold:
            self._switch_to(BackendMode.ANTHROPIC)
        return self.mode

    def _switch_to(self, mode: BackendMode) -> None:
        self.mode = mode
        self.last_switch_at = time.time()
