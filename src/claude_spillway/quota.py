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


@dataclass(frozen=True)
class OllamaSnapshot:
    """Ollama Cloud usage, read from the account's usage endpoint.

    Ollama reports a session window and a weekly window as 0-1 utilization
    ratios, plus a per-model request count. Unlike Anthropic it reports **no
    reset times**, so anything that needs to know when a window turns over
    cannot be answered for this backend.

    Ollama Cloudの使用状況(アカウントの使用量エンドポイントから取得)。
    Ollamaはセッション窓と週次窓を0〜1の使用率で返し、モデル別リクエスト数も
    併せて返す。Anthropicと違い**リセット時刻は返さない**ため、窓がいつ切り替わる
    かを前提にした判断はこのバックエンドについては行えない。
    """

    session_utilization: float | None
    weekly_utilization: float | None
    observed_at: float
    #: Requests per model within the weekly window, most used first.
    #: 週次窓におけるモデル別リクエスト数(多い順)。
    weekly_models: tuple[tuple[str, int], ...] = ()

    def remaining_ratio(self) -> float | None:
        """Return the tightest remaining ratio across the known windows.

        既知の窓のうち最も逼迫している残量比率を返す。
        """
        ratios = [
            1.0 - value
            for value in (self.session_utilization, self.weekly_utilization)
            if value is not None
        ]
        return min(ratios) if ratios else None

    def weekly_remaining_ratio(self) -> float | None:
        """Return the weekly headroom alone, used when balancing the two backends.

        週次窓の残量のみを返す。2つのバックエンドを比較する際に用いる。
        """
        if self.weekly_utilization is None:
            return None
        return 1.0 - self.weekly_utilization


def parse_ollama_usage(payload: dict[str, Any]) -> OllamaSnapshot:
    """Build an :class:`OllamaSnapshot` from the Ollama usage endpoint response.

    Like the Anthropic usage endpoint this is not part of a published API, so
    every field is read defensively and a shape change degrades to "no data".
    The utilization values are already 0-1 ratios here - no percent conversion.

    Ollamaの使用量エンドポイントのレスポンスから :class:`OllamaSnapshot` を作る。
    Anthropic側と同様に公開APIではないため全フィールドを防御的に読み、形が変わって
    も「データなし」に縮退させる。使用率は既に0〜1の比率なので変換は不要。
    """
    limits = payload.get("limits") or {}
    session = limits.get("session") or {}
    weekly = limits.get("weekly") or {}

    models: list[tuple[str, int]] = []
    for entry in weekly.get("models") or []:
        if isinstance(entry, dict) and isinstance(entry.get("request_count"), int):
            models.append((str(entry.get("name", "?")), entry["request_count"]))
    models.sort(key=lambda item: item[1], reverse=True)

    return OllamaSnapshot(
        session_utilization=_ratio(session.get("usage")),
        weekly_utilization=_ratio(weekly.get("usage")),
        observed_at=time.time(),
        weekly_models=tuple(models),
    )


def _ratio(value: Any) -> float | None:
    """Accept a 0-1 utilization value, rejecting anything else.

    0〜1の使用率だけを受け付け、それ以外は None にする。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


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


class RoutingPolicy(str, Enum):
    """How to choose between the two backends while neither is critical.

    どちらも逼迫していない状況で、2つのバックエンドをどう選ぶか。
    """

    #: Stay on Anthropic until it runs low, then fall over. The original behaviour.
    #: Anthropicが逼迫するまで使い続け、逼迫したら切り替える。従来の挙動。
    ANTHROPIC_FIRST = "anthropic_first"
    #: Spread load by preferring whichever side has more of its weekly window left.
    #: 週次窓の残量が多い方を優先して負荷を分散する。
    WEEKLY_BALANCE = "weekly_balance"


class QuotaTracker:
    """State machine holding the current backend mode and the routing decision.

    Keeping fallback_threshold_pct and recovery_threshold_pct apart creates
    hysteresis, which stops the mode from flapping while the remaining ratio
    hovers around a single threshold. The same idea guards the balancing policy
    (a margin) and the reverse failover (a cooldown).

    現在のバックエンドモードを保持し、ルーティングを判断する状態機械。
    fallback_threshold_pct と recovery_threshold_pct の間にヒステリシスを
    設けることで、残量が閾値付近で微振動して頻繁に切り替わる(flapping)のを防ぐ。
    同じ考え方をバランシング(マージン)と逆フェイルオーバー(クールダウン)にも
    適用している。
    """

    def __init__(
        self,
        fallback_threshold_pct: float,
        recovery_threshold_pct: float,
        policy: RoutingPolicy = RoutingPolicy.ANTHROPIC_FIRST,
        ollama_min_remaining_pct: float = 5.0,
        balance_session_floor_pct: float = 50.0,
        balance_margin_pct: float = 10.0,
        ollama_failure_threshold: int = 5,
        reverse_failover_min_5h_pct: float = 10.0,
        reverse_failover_cooldown_seconds: float = 300.0,
    ) -> None:
        if recovery_threshold_pct <= fallback_threshold_pct:
            raise ValueError(
                "recovery_threshold_pct must be greater than fallback_threshold_pct "
                f"(got fallback={fallback_threshold_pct}, recovery={recovery_threshold_pct})"
            )
        self._fallback_threshold = fallback_threshold_pct / 100.0
        self._recovery_threshold = recovery_threshold_pct / 100.0
        self._policy = policy
        self._ollama_min_remaining = ollama_min_remaining_pct / 100.0
        self._balance_session_floor = balance_session_floor_pct / 100.0
        self._balance_margin = balance_margin_pct / 100.0
        self._ollama_failure_threshold = ollama_failure_threshold
        self._reverse_min_5h = reverse_failover_min_5h_pct / 100.0
        self._reverse_cooldown = reverse_failover_cooldown_seconds

        self.mode: BackendMode = BackendMode.ANTHROPIC
        self.last_snapshot: QuotaSnapshot | None = None
        self.ollama_snapshot: OllamaSnapshot | None = None
        self.last_switch_at: float = time.time()
        #: Why the current mode was chosen, for the status endpoint and the TUI.
        #: 現在のモードを選んだ理由(status APIとTUIでの表示用)。
        self.last_reason: str = "initial"
        #: Ollama is off-limits until this time, after a burst of failures.
        #: 連続失敗を受けて、この時刻まではOllamaを使わない。
        self._ollama_blocked_until: float = 0.0

    @property
    def policy(self) -> RoutingPolicy:
        return self._policy

    def observe(self, snapshot: QuotaSnapshot) -> BackendMode:
        """Take in a new Anthropic reading and re-decide the backend.

        新しいAnthropicの観測値を取り込み、バックエンドを再判定する。
        """
        self.last_snapshot = snapshot
        return self._reevaluate()

    def observe_ollama(self, snapshot: OllamaSnapshot) -> BackendMode:
        """Take in a new Ollama reading and re-decide the backend.

        新しいOllamaの観測値を取り込み、バックエンドを再判定する。
        """
        self.ollama_snapshot = snapshot
        return self._reevaluate()

    def note_ollama_failures(self, consecutive_failures: int) -> BackendMode:
        """React to Ollama failing repeatedly by going back to Anthropic.

        Ollama Cloud can get slow or fail outright when a model is busy, and a
        proxy that keeps sending there is worse than one that spends a little
        Anthropic quota. Only worth doing while Anthropic has enough of its 5h
        window left to actually serve the traffic.

        Ollamaが連続で失敗した場合にAnthropicへ戻す。
        Ollama Cloudは特定モデルへのアクセス集中時に遅延したり失敗したりする。
        そこへ送り続けるくらいならAnthropicのquotaを少し使う方が良い。ただし
        Anthropicの5時間窓に実際に捌けるだけの残量がある場合に限る。
        """
        if consecutive_failures < self._ollama_failure_threshold:
            return self.mode
        remaining_5h = self._anthropic_window(anthropic_5h=True)
        if remaining_5h is None or remaining_5h < self._reverse_min_5h:
            # Nowhere better to go; staying put beats thrashing.
            # 逃げ先が無いため、無理に切り替えず現状を維持する。
            return self.mode
        self._ollama_blocked_until = time.time() + self._reverse_cooldown
        if self.mode is BackendMode.FALLBACK:
            self._switch_to(BackendMode.ANTHROPIC, "ollama_failures")
        return self.mode

    # ------------------------------------------------------------------
    # 判定ロジック / decision logic
    # ------------------------------------------------------------------

    def _reevaluate(self) -> BackendMode:
        now = time.time()
        ollama_remaining = (
            self.ollama_snapshot.remaining_ratio() if self.ollama_snapshot is not None else None
        )

        # 1. Guards that rule Ollama out entirely.
        #    Ollamaを使えなくする条件(最優先)。
        if now < self._ollama_blocked_until:
            return self._ensure(BackendMode.ANTHROPIC, "ollama_cooldown")
        if ollama_remaining is not None and ollama_remaining < self._ollama_min_remaining:
            return self._ensure(BackendMode.ANTHROPIC, "ollama_exhausted")

        anthropic_remaining = (
            self.last_snapshot.remaining_ratio() if self.last_snapshot is not None else None
        )
        if anthropic_remaining is None:
            # Nothing observed yet; keep whatever we are doing.
            # まだ何も観測できていないため現状維持。
            return self.mode

        # 2. Hard guard: Anthropic is critically low, so fail over regardless of policy.
        #    ハードガード: Anthropicが逼迫しているため、ポリシーに関係なく切り替える。
        if anthropic_remaining < self._fallback_threshold:
            return self._ensure(BackendMode.FALLBACK, "anthropic_low")

        # 3. Policy, applied only while neither side is critical.
        #    ポリシー適用(どちらも逼迫していない場合のみ)。
        if self._policy is RoutingPolicy.WEEKLY_BALANCE:
            target = self._weekly_balance_target()
            if target is not None:
                return self._ensure(target, "weekly_balance")

        # 4. Hysteresis: come back to Anthropic once it has recovered enough.
        #    ヒステリシス: 十分に回復したらAnthropicへ戻す。
        if self.mode is BackendMode.FALLBACK and anthropic_remaining >= self._recovery_threshold:
            return self._ensure(BackendMode.ANTHROPIC, "anthropic_recovered")
        return self.mode

    def _weekly_balance_target(self) -> BackendMode | None:
        """Pick the side with more weekly headroom, or ``None`` if not applicable.

        Only engages while both short windows are comfortable - balancing is
        about spending the weekly budget evenly, not about rescuing a backend
        that is about to run out right now. The margin provides the hysteresis:
        we only move if the other side is meaningfully better off.

        週次残量が多い方を返す。適用できない場合は ``None``。
        短い窓が両方とも十分に余裕がある間だけ働く。バランシングは週次予算を
        均す仕組みであって、いま枯渇しかけている側を救うためのものではないため。
        マージンがヒステリシスの役割を果たし、相手側が有意に余裕がある場合のみ
        切り替える。
        """
        if self.ollama_snapshot is None or self.last_snapshot is None:
            return None
        anthropic_session = self._anthropic_window(anthropic_5h=True)
        ollama_session = (
            None
            if self.ollama_snapshot.session_utilization is None
            else 1.0 - self.ollama_snapshot.session_utilization
        )
        if anthropic_session is None or ollama_session is None:
            return None
        if anthropic_session < self._balance_session_floor or ollama_session < self._balance_session_floor:
            return None

        anthropic_weekly = self._anthropic_window(anthropic_5h=False)
        ollama_weekly = self.ollama_snapshot.weekly_remaining_ratio()
        if anthropic_weekly is None or ollama_weekly is None:
            return None

        if self.mode is BackendMode.ANTHROPIC:
            current, other, other_mode = anthropic_weekly, ollama_weekly, BackendMode.FALLBACK
        else:
            current, other, other_mode = ollama_weekly, anthropic_weekly, BackendMode.ANTHROPIC
        return other_mode if other - current >= self._balance_margin else self.mode

    def _anthropic_window(self, *, anthropic_5h: bool) -> float | None:
        """Remaining ratio of one Anthropic window, or ``None`` when unknown.

        Anthropicの片方の窓の残量比率。不明なら ``None``。
        """
        if self.last_snapshot is None:
            return None
        utilization = (
            self.last_snapshot.utilization_5h if anthropic_5h else self.last_snapshot.utilization_7d
        )
        return None if utilization is None else 1.0 - utilization

    def _ensure(self, mode: BackendMode, reason: str) -> BackendMode:
        if mode is not self.mode:
            self._switch_to(mode, reason)
        else:
            self.last_reason = reason
        return self.mode

    def _switch_to(self, mode: BackendMode, reason: str) -> None:
        self.mode = mode
        self.last_reason = reason
        self.last_switch_at = time.time()
