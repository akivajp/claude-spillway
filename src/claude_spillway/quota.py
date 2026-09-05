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
        """Return how much subscription quota is left, as the tightest window.

        The 5-hour and weekly windows are what "running out of quota" means:
        they refill on a schedule Anthropic reports, and once spent there is
        nowhere to go but the other backend.

        ``anthropic-ratelimit-{requests,tokens}-remaining`` are a different
        quantity - short-term buckets that refill continuously - and a low
        reading there means "wait a moment", not "the quota is gone". Mixing
        them in made an ordinary burst look like exhaustion and moved traffic
        off a backend with most of its quota intact, so they are consulted only
        when no window is reported at all, which is the API-key billing case
        where they are the only signal there is.

        サブスクリプションのquota残量を、最も逼迫している窓の値として返す。
        「quotaを使い切る」とは5時間窓と週次窓のことであり、これらはAnthropicが
        リセット時刻を明示する枠で、尽きたら他のバックエンドへ行くしかない。

        ``anthropic-ratelimit-{requests,tokens}-remaining`` は別物で、継続的に
        補充される短期バケツである。ここが少ないのは「少し待て」であって
        「枠が尽きた」ではない。同一視すると通常のバースト利用が枯渇に見え、
        quotaがほぼ残っているバックエンドからトラフィックを逃がしてしまうため、
        窓が1つも読めない場合(=これらが唯一の信号であるAPIキー課金の場合)に
        限って参照する。
        """
        windows = [
            1.0 - value
            for value in (self.utilization_5h, self.utilization_7d)
            if value is not None
        ]
        if windows:
            return min(windows)
        buckets = [
            value
            for value in (self.requests_remaining_ratio, self.tokens_remaining_ratio)
            if value is not None
        ]
        return min(buckets) if buckets else None


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
    #: ESTIMATED reset times, derived by assuming a window started when its
    #: utilization last rose. A rising utilization marks the start of a fresh
    #: window, so "start + window length" bounds when it must reset - the
    #: estimate is a latest-possible-reset, not the true value. ``None`` until
    #: the first rise is observed. Kept separate from Anthropic's reported
    #: resets because only one of the two is a measurement.
    #: 予測リセット時刻。利用率が最後に上昇した時刻を窓の始点とみなし、
    #: 「始点＋窓長」をリセット時刻とみなす。利用率の上昇は新しい窓の始まりを
    #: 意味するため、この予測値は「リセットが確実に済んでいる上限時刻」であり
    #: 真の値ではない。最初の上昇を観測するまで None。
    #: Anthropicの実測値と混同しないよう別フィールドにしている。
    estimated_session_reset: float | None = None
    estimated_weekly_reset: float | None = None

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


#: Ollama's session window is at most five hours and its weekly window at most
#: seven days. If Ollama ever starts reporting true reset times, both this pair
#: and the estimator that feeds on them go away - see .github/TODO.md.
#: Ollamaのセッション窓は最長5時間、週次窓は最長7日。Ollamaが真のリセット時刻を
#: 返すようになったら、このペアとそれを食う推定器は消える(.github/TODO.md 参照)。
OLLAMA_SESSION_WINDOW_SECONDS = 5 * 3600
OLLAMA_WEEKLY_WINDOW_SECONDS = 7 * 24 * 3600


class OllamaResetEstimator:
    """Bound when Ollama's windows must reset, from utilization movements alone.

    A window's utilization cannot fall until the window resets, so the first
    observed rise marks the start of a fresh window: the reading before it
    belonged to a window that had just ended. "That rise + the window's maximum
    length" is therefore the latest the reset could have happened, and the
    current window cannot outlive it. This is an upper bound, not the truth -
    the real reset may be much earlier, which is why every estimate carries a
    tilde in the TUI.

    利用率は窓がリセットされない限り下がらない。したがって最初に観測した上昇は
    新しい窓の始点を示し、その直前の値は直前に終わった窓に属していたことになる。
    「上昇時刻＋窓の最大長」はリセットが起こっていた可能性が確実にある最遅時刻で
    あり、現在の窓がそれを超えて存続することはない。これは上限であり真値ではない。
    そのためTUIではすべて波ダッシュ付きで表示する。
    """

    def __init__(self) -> None:
        # 前回の利用率。上昇検出のために保持する。
        # Previous readings, kept to detect a rise.
        self._prev_session: float | None = None
        self._prev_weekly: float | None = None
        # 最後に立てた予測値。上昇が無い読み取りでも保持する(窓はリセットまで
        # 有効なため、予測値も有効であり続ける)。
        # The last bound we derived, kept across readings that show no rise:
        # the window is still running, so its bound is still the bound.
        self._last_session_reset: float | None = None
        self._last_weekly_reset: float | None = None

    def update(self, snapshot: OllamaSnapshot) -> OllamaSnapshot:
        """Return ``snapshot`` with estimated reset times attached.

        予測リセット時刻を付与したスナップショットを返す。
        """
        session_reset = self._advance(
            prev=self._prev_session,
            current=snapshot.session_utilization,
            now=snapshot.observed_at,
            window_length=OLLAMA_SESSION_WINDOW_SECONDS,
            last_estimate=self._last_session_reset,
        )
        weekly_reset = self._advance(
            prev=self._prev_weekly,
            current=snapshot.weekly_utilization,
            now=snapshot.observed_at,
            window_length=OLLAMA_WEEKLY_WINDOW_SECONDS,
            last_estimate=self._last_weekly_reset,
        )
        self._last_session_reset = session_reset
        self._last_weekly_reset = weekly_reset
        if snapshot.session_utilization is not None:
            self._prev_session = snapshot.session_utilization
        if snapshot.weekly_utilization is not None:
            self._prev_weekly = snapshot.weekly_utilization

        return OllamaSnapshot(
            session_utilization=snapshot.session_utilization,
            weekly_utilization=snapshot.weekly_utilization,
            observed_at=snapshot.observed_at,
            weekly_models=snapshot.weekly_models,
            estimated_session_reset=session_reset,
            estimated_weekly_reset=weekly_reset,
        )

    def _advance(
        self,
        *,
        prev: float | None,
        current: float | None,
        now: float,
        window_length: float,
        last_estimate: float | None,
    ) -> float | None:
        """Update one window's tracker and return its estimated reset time.

        一つの窓の状態を更新し、予測リセット時刻を返す。
        """
        if current is None:
            # 欠損読み取りでも既存の予測は捨てない。窓は進み続けている。
            # A missing reading does not invalidate an existing bound.
            return last_estimate
        if prev is not None and current > prev:
            # 上昇を検出: 直前の読み値は直前に終わった窓のものだった。
            # A rise: the previous reading belonged to a window that just ended.
            return now + window_length
        if last_estimate is not None and last_estimate >= now + window_length:
            # ありえないほど古い予測は窓長を超えているため、理論上は起こらない。
            # 念のため残す(古い上限のままだと窓が切り替わった後に誤表示する)。
            # A bound older than one window length cannot happen; keep the
            # guard so a stale estimate cannot outlive its own window.
            return None
        # 上昇が観測されるまで、窓の残り時間は未知のままなので予測もしない。
        # Until a rise is seen, how long the window has been running is unknown,
        # so there is nothing to estimate yet.
        return last_estimate


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


def _clamped_ratio(value: float) -> float:
    """Pin a utilization reading into 0..1.

    Anthropic reports utilization above 1.0 when a window is overdrawn — the
    source of the "-12% remaining" the TUI showed. There is nothing left in a
    window like that, so clamping to exactly 1.0 (i.e. 0 remaining) both fixes
    the display and makes the ordinary threshold logic treat it as exhausted,
    which it is.

    利用率を0〜1に収める。窓が枠超過した場合、Anthropicは1.0超の値を返す
    (TUIに表示されていた「残量-12%」の原因)。こうした窓には残量が無いため、
    厳密に1.0(=残量0)へクランプすることで表示が直るだけでなく、通常の閾値判定が
    その窓を枯渇扱いにする。実際に枯渇しているのだから、それが正しい。
    """
    return max(0.0, min(1.0, value))


def _parse_remaining_ratio(
    headers: httpx.Headers, remaining_name: str, limit_name: str
) -> float | None:
    remaining = _parse_float(headers, remaining_name)
    limit = _parse_float(headers, limit_name)
    if remaining is None or limit is None or limit <= 0:
        return None
    # remaining/limit は0超になりうる(枠超過)ためクランプする。
    # The ratio can exceed 1 when a window is overdrawn, so clamp it.
    return _clamped_ratio(remaining / limit)


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
        # 利用率ヘッダーは枠超過で1.0を超えてくるため、ここでクランプする。
        # Utilization headers exceed 1.0 when a window is overdrawn; clamp here.
        utilization_5h=(
            None
            if (v := _parse_float(headers, "anthropic-ratelimit-unified-5h-utilization")) is None
            else _clamped_ratio(v)
        ),
        utilization_7d=(
            None
            if (v := _parse_float(headers, "anthropic-ratelimit-unified-7d-utilization")) is None
            else _clamped_ratio(v)
        ),
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
    #: Prefer whichever side can serve longer relative to how long is left in its
    #: windows - the escape hatch for a backend that is burning its quota faster
    #: than its reset will allow.
    #: 窓の残り時間に対してどちらが長く捌けるかを比べ、リセットまでに使い切りそうな
    #: 側を避けるための安全弁。
    BURN_RATE_BALANCE = "burn_rate_balance"


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
        anthropic_priority_weight: float = 1.1,
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
        # バーンレート均衡でのAnthropic側の優先重み。1.0なら同等扱い。
        # Anthropic's weight in the burn-rate comparison; 1.0 means neutral.
        self._anthropic_weight = anthropic_priority_weight
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
        #: True while the latest Anthropic fact is a 429. Rate-limit rejections
        #: carry no utilization headers, so without this flag "no data" would
        #: keep the proxy parked on a backend that refuses every request. The
        #: flag stands in for a reading until one with real numbers arrives.
        #: 直近のAnthropic側の事実が429である間立つフラグ。レート制限の拒否応答に
        #: は使用率ヘッダーが載らないため、このフラグが無いと「データなし」のまま
        #: 全リクエストを拒否し続けるバックエンドに留まってしまう。実数値の観測が
        #: 届くまでの代用として機能する。
        self._anthropic_rate_limited: bool = False

    @property
    def policy(self) -> RoutingPolicy:
        return self._policy

    def observe(self, snapshot: QuotaSnapshot) -> BackendMode:
        """Take in a new Anthropic reading and re-decide the backend.

        A reading with actual numbers outranks a 429: it is newer evidence
        about the same window, so the rate-limit flag retires once one shows
        up. Header-less snapshots (a relayed 429, a failed probe) leave the
        flag alone - they say nothing either way.

        新しいAnthropicの観測値を取り込み、バックエンドを再判定する。
        実数値のある観測は429より新しい証拠であるため、実数値が届いた時点で
        レート制限フラグは役目を終える。ヘッダーの無い観測(中継された429、
        失敗したプローブ)はどちらとも言えないため、フラグには触れない。
        """
        self.last_snapshot = snapshot
        if snapshot.remaining_ratio() is not None:
            self._anthropic_rate_limited = False
        return self._reevaluate()

    def observe_ollama(self, snapshot: OllamaSnapshot) -> BackendMode:
        """Take in a new Ollama reading and re-decide the backend.

        新しいOllamaの観測値を取り込み、バックエンドを再判定する。
        """
        self.ollama_snapshot = snapshot
        return self._reevaluate()

    def note_anthropic_rate_limited(self) -> BackendMode:
        """Record that Anthropic answered 429, then re-decide the backend.

        A 429 means one of Anthropic's windows is spent, so the traffic has
        somewhere better to go - but only if Ollama can take it, which the
        re-evaluation below decides. Until a reading with real numbers lands,
        the flagged state counts Anthropic as having nothing left.

        Anthropicが429を返したことを記録し、バックエンドを再判定する。
        429はいずれかの窓が使い切ったことを意味するため、行き先があるなら
        そちらへ移るべきだが、移れるかどうかはOllama側の残量次第であり、
        その判定は下の再評価に委ねる。実数値の観測が届くまで、Anthropicは
        残量0として扱う。
        """
        self._anthropic_rate_limited = True
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
        # 直近のAnthropic側の事実が429なら、古い5時間窓の読み値は信用できない。
        # If the latest Anthropic fact is a 429, the stale 5h reading is moot.
        remaining_5h = (
            None if self._anthropic_rate_limited else self._anthropic_window(anthropic_5h=True)
        )
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
        anthropic_remaining = (
            self.last_snapshot.remaining_ratio() if self.last_snapshot is not None else None
        )
        if self._anthropic_rate_limited:
            # 429は実測ヘッダーより新しい「枯渇」の証拠。実数値が届くまで残0扱い。
            # A 429 is newer exhaustion evidence than any header reading; count
            # Anthropic as empty until one with real numbers lands.
            anthropic_remaining = 0.0

        # 1. Guards that rule Ollama out entirely.
        #    Ollamaを使えなくする条件(最優先)。
        if now < self._ollama_blocked_until:
            return self._ensure(BackendMode.ANTHROPIC, "ollama_cooldown")
        # Below Ollama's floor, but stand down only while Anthropic reads
        # and holds strictly more. When Anthropic is itself exhausted (or
        # unreadable) its last drops are worthless, so spending Ollama's
        # remaining few percent beats relaying into guaranteed rejections.
        # Ollamaが下限を下回っても、Anthropicが読めてかつより余裕がある場合に
        # 限って使用を断念する。Anthropic自身が枯渇(または読めない)場合、
        # そちらに送り続けるのは確実な拒否であって、Ollamaの残り数%を使う
        # 方がまだましである。
        if (
            ollama_remaining is not None
            and ollama_remaining < self._ollama_min_remaining
            and anthropic_remaining is not None
            and anthropic_remaining > ollama_remaining
        ):
            return self._ensure(BackendMode.ANTHROPIC, "ollama_exhausted")

        if anthropic_remaining is None:
            # Nothing observed yet; keep whatever we are doing.
            # まだ何も観測できていないため現状維持。
            return self.mode

        # 2. Hard guard: Anthropic is critically low, so fail over regardless of policy.
        #    ハードガード: Anthropicが逼迫しているため、ポリシーに関係なく切り替える。
        if anthropic_remaining < self._fallback_threshold:
            reason = (
                "anthropic_rate_limited" if self._anthropic_rate_limited else "anthropic_low"
            )
            return self._ensure(BackendMode.FALLBACK, reason)

        # 3. Policy, applied only while neither side is critical.
        #    ポリシー適用(どちらも逼迫していない場合のみ)。
        if self._policy is RoutingPolicy.WEEKLY_BALANCE:
            target = self._weekly_balance_target()
            if target is not None:
                return self._ensure(target, "weekly_balance")
        elif self._policy is RoutingPolicy.BURN_RATE_BALANCE:
            target = self._burn_rate_target()
            if target is not None:
                return self._ensure(target, "burn_rate_balance")

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

    def _burn_rate_target(self) -> BackendMode | None:
        """Pick the side that can serve longer relative to its windows' remaining time.

        For each window compute ``remaining / (time left until reset / window
        length)`` and adopt the smallest - the window that will run dry soonest
        relative to how long it still has to last. A reset time we do not know
        (Ollama never reports one) means assuming the window just started, i.e.
        the most generous reading, so an unknown never manufactures urgency.

        Anthropic gets a weight above 1.0 so that, at parity, we still favour
        spending the Claude subscription we are already paying for.

        各窓について「残量 ÷ (リセットまでの時間 ÷ 窓の全長)」を求め、最小値
        (相対的に最も早く枯渇する窓)を採用する。リセット時刻が不明な場合
        (Ollamaは常にこれに該当)は「窓が始まったばかり」と仮定して最も甘く見積もる。
        不明であることを根拠に焦りを作らないためである。

        Anthropic側には1.0超の重みを掛ける。拮抗した場合でも、支払っている
        Claudeサブスクリプションの枠を優先して使うためである。
        """
        if self.ollama_snapshot is None or self.last_snapshot is None:
            return None
        anthropic = self._burn_rate(
            session=self._anthropic_window(anthropic_5h=True),
            session_reset=self.last_snapshot.reset_5h,
            weekly=self._anthropic_window(anthropic_5h=False),
            weekly_reset=self.last_snapshot.reset_7d,
        )
        # Ollamaはリセット時刻を返さないため、両窓とも「始まったばかり」扱いになる。
        # Ollama never reports resets, so both windows take the generous reading.
        ollama_snap = self.ollama_snapshot
        ollama = self._burn_rate(
            session=None if ollama_snap.session_utilization is None else 1.0 - ollama_snap.session_utilization,
            session_reset=None,
            weekly=ollama_snap.weekly_remaining_ratio(),
            weekly_reset=None,
        )
        if anthropic is None or ollama is None:
            return None
        if not self._both_session_windows_comfortable():
            return None

        anthropic *= self._anthropic_weight
        # マージン(ヒステリシス)を効かせ、拮抗した状態での振動を防ぐ。
        # Apply the margin so near-parity does not make the mode oscillate.
        if self.mode is BackendMode.ANTHROPIC:
            current, other, other_mode = anthropic, ollama, BackendMode.FALLBACK
        else:
            current, other, other_mode = ollama, anthropic, BackendMode.ANTHROPIC
        if current >= other * (1.0 + self._balance_margin):
            return self.mode
        return other_mode if other > current else self.mode

    def _burn_rate(
        self,
        *,
        session: float | None,
        session_reset: float | None,
        weekly: float | None,
        weekly_reset: float | None,
    ) -> float | None:
        """Smallest time-normalized remaining ratio across the two windows.

        各窓の時間正規化残量のうち最小のものを返す。
        """
        window_lengths = {"session": 5 * 3600, "weekly": 7 * 24 * 3600}
        rates: list[float] = []
        for remaining, reset, length in (
            (session, session_reset, window_lengths["session"]),
            (weekly, weekly_reset, window_lengths["weekly"]),
        ):
            if remaining is None:
                continue
            fraction_left = 1.0 if reset is None else max(0.0, reset - time.time()) / length
            # リセット済み(残時間0)でも「始まったばかり」と同じ扱いにする。
            # 次の観測で新しい値が入るため、ここで焦りを作る必要はない。
            # A window that has already reset reads as freshly started too; the
            # next observation will carry the new value, so no urgency here.
            rates.append(remaining / max(fraction_left, 1e-9))
        return min(rates) if rates else None

    def _both_session_windows_comfortable(self) -> bool:
        """True when both short windows hold at least the balance floor.

        短い窓が両方ともバランス開始の下限を満たす場合に True。
        """
        if self.ollama_snapshot is None or self.ollama_snapshot.session_utilization is None:
            return False
        if self.last_snapshot is None or self.last_snapshot.utilization_5h is None:
            return False
        ollama_session = 1.0 - self.ollama_snapshot.session_utilization
        anthropic_session = 1.0 - self.last_snapshot.utilization_5h
        return (
            anthropic_session >= self._balance_session_floor
            and ollama_session >= self._balance_session_floor
        )

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
