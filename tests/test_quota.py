"""Unit tests for quota.py. / quota.py の単体テスト。"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import pytest

from claude_spillway.quota import (
    OLLAMA_SESSION_WINDOW_SECONDS,
    OLLAMA_WEEKLY_WINDOW_SECONDS,
    SOURCE_HEADERS,
    SOURCE_USAGE_API,
    BackendMode,
    OllamaResetEstimator,
    OllamaSnapshot,
    QuotaSnapshot,
    QuotaTracker,
    RoutingPolicy,
    parse_ollama_usage,
    parse_quota_headers,
    parse_usage_payload,
)


def test_parse_quota_headers_unified() -> None:
    headers = httpx.Headers(
        {
            "anthropic-ratelimit-unified-5h-utilization": "0.92",
            "anthropic-ratelimit-unified-7d-utilization": "0.40",
        }
    )
    snapshot = parse_quota_headers(headers)
    assert snapshot.utilization_5h == pytest.approx(0.92)
    assert snapshot.utilization_7d == pytest.approx(0.40)
    # The 5h window is the tightest one (8% left), so the minimum wins.
    # 5時間窓の方が逼迫している(残8%) -> 最小値が採用される
    assert snapshot.remaining_ratio() == pytest.approx(0.08)


def test_parse_quota_headers_requests_tokens() -> None:
    headers = httpx.Headers(
        {
            "anthropic-ratelimit-requests-limit": "1000",
            "anthropic-ratelimit-requests-remaining": "50",
            "anthropic-ratelimit-tokens-limit": "100000",
            "anthropic-ratelimit-tokens-remaining": "40000",
        }
    )
    snapshot = parse_quota_headers(headers)
    assert snapshot.requests_remaining_ratio == pytest.approx(0.05)
    assert snapshot.tokens_remaining_ratio == pytest.approx(0.4)
    assert snapshot.remaining_ratio() == pytest.approx(0.05)


def test_short_term_buckets_do_not_count_as_spent_quota() -> None:
    """A drained per-minute bucket must not read as an exhausted subscription.

    ``anthropic-ratelimit-{requests,tokens}-remaining`` refill continuously, so
    a low reading means "wait a moment", not "the quota is gone". Counting them
    as quota moved traffic off a backend with 87% of its window left, and into
    an Ollama that was itself out - the whole failover was spurious.

    毎分補充されるバケツの枯渇を、サブスクリプション枠の枯渇と読み違えないこと。
    ``anthropic-ratelimit-{requests,tokens}-remaining`` は継続的に補充されるため、
    値が小さいのは「少し待て」であって「枠が尽きた」ではない。これをquotaとして
    数えた結果、窓が87%残っているのにトラフィックを、しかも自身も枯渇している
    Ollamaへ逃がしていた。フェイルオーバーそのものが誤りだった。
    """
    headers = httpx.Headers(
        {
            "anthropic-ratelimit-unified-5h-utilization": "0.13",
            "anthropic-ratelimit-unified-7d-utilization": "0.01",
            "anthropic-ratelimit-requests-limit": "1000",
            "anthropic-ratelimit-requests-remaining": "420",
            "anthropic-ratelimit-tokens-limit": "200000",
            "anthropic-ratelimit-tokens-remaining": "12000",
        }
    )
    snapshot = parse_quota_headers(headers)
    # 表示用の値としては今までどおり読める。
    # Still read for display, as before.
    assert snapshot.tokens_remaining_ratio == pytest.approx(0.06)
    # 判定に使う残量は窓のみを見る(7d残99% と 5h残87% の小さい方)。
    # The ratio used for routing looks at the windows alone.
    assert snapshot.remaining_ratio() == pytest.approx(0.87)

    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    assert tracker.observe(snapshot) is BackendMode.ANTHROPIC


def test_buckets_are_still_used_when_no_window_is_reported() -> None:
    """With API-key billing the buckets are the only signal, so they must count.

    APIキー課金では窓ヘッダーが付かず、バケツが唯一の信号であるため参照すること。
    """
    snapshot = parse_quota_headers(
        httpx.Headers(
            {
                "anthropic-ratelimit-requests-limit": "1000",
                "anthropic-ratelimit-requests-remaining": "50",
            }
        )
    )
    assert snapshot.remaining_ratio() == pytest.approx(0.05)
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    assert tracker.observe(snapshot) is BackendMode.FALLBACK


def test_parse_quota_headers_missing() -> None:
    snapshot = parse_quota_headers(httpx.Headers({}))
    assert snapshot.remaining_ratio() is None


def test_tracker_requires_hysteresis_ordering() -> None:
    with pytest.raises(ValueError):
        QuotaTracker(fallback_threshold_pct=20.0, recovery_threshold_pct=10.0)


def test_tracker_switches_to_fallback_below_threshold() -> None:
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    assert tracker.mode is BackendMode.ANTHROPIC

    headers = httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "0.95"})
    mode = tracker.observe(parse_quota_headers(headers))
    assert mode is BackendMode.FALLBACK
    assert tracker.mode is BackendMode.FALLBACK


def test_tracker_hysteresis_prevents_flapping() -> None:
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    tracker.observe(parse_quota_headers(httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "0.95"})))
    assert tracker.mode is BackendMode.FALLBACK

    # Back up to 15% left, still short of the 20% recovery threshold: stay in fallback.
    # 残り15% まで戻ったが、recovery閾値(残り20%)にはまだ届いていないので切り戻らない
    tracker.observe(parse_quota_headers(httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "0.85"})))
    assert tracker.mode is BackendMode.FALLBACK

    # Recovered to 25% left, so we switch back.
    # 残り25% まで回復したので切り戻る
    tracker.observe(parse_quota_headers(httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "0.75"})))
    assert tracker.mode is BackendMode.ANTHROPIC


def test_tracker_ignores_snapshot_without_data() -> None:
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    mode = tracker.observe(parse_quota_headers(httpx.Headers({})))
    assert mode is BackendMode.ANTHROPIC


def test_parses_reset_headers_in_both_formats() -> None:
    """Reset times may arrive as RFC 3339 or as epoch seconds; accept both.

    リセット時刻はRFC 3339でもエポック秒でも来うるため、両方を受け付けること。
    """
    headers = httpx.Headers(
        {
            "anthropic-ratelimit-unified-5h-utilization": "0.46",
            "anthropic-ratelimit-unified-5h-reset": "2026-09-05T11:29:59.839190+00:00",
            "anthropic-ratelimit-unified-7d-reset": "1788600000",
        }
    )
    snapshot = parse_quota_headers(headers)
    expected = datetime(2026, 9, 5, 11, 29, 59, 839190, tzinfo=UTC).timestamp()
    assert snapshot.reset_5h == pytest.approx(expected)
    assert snapshot.reset_7d == pytest.approx(1788600000.0)
    assert snapshot.source == SOURCE_HEADERS


def test_reset_headers_absent_or_unparsable() -> None:
    snapshot = parse_quota_headers(httpx.Headers({"anthropic-ratelimit-unified-5h-reset": "soon"}))
    assert snapshot.reset_5h is None
    assert snapshot.reset_7d is None


def test_parse_usage_payload_converts_percent_to_ratio() -> None:
    """The usage endpoint reports percentages; the headers report ratios.

    Mixing them up would misread quota by 100x, so pin the conversion down.

    使用量エンドポイントはパーセント、ヘッダーは比率で返す。取り違えると
    100倍ずれた値を読むことになるため、変換をテストで固定する。
    """
    snapshot = parse_usage_payload(
        {
            "five_hour": {"utilization": 46.0, "resets_at": "2026-09-05T11:29:59+00:00"},
            "seven_day": {"utilization": 12.0, "resets_at": None},
        }
    )
    assert snapshot.utilization_5h == pytest.approx(0.46)
    assert snapshot.utilization_7d == pytest.approx(0.12)
    # 最も逼迫しているのは5時間窓(残54%) / the 5h window is the tightest at 54% left
    assert snapshot.remaining_ratio() == pytest.approx(0.54)
    assert snapshot.reset_5h is not None
    assert snapshot.reset_7d is None
    assert snapshot.source == SOURCE_USAGE_API


def test_parse_usage_payload_tolerates_missing_or_odd_shapes() -> None:
    """A shape change in this unpublished endpoint must mean "no data", not a crash.

    非公開エンドポイントの形が変わっても、例外ではなく「データなし」になること。
    """
    assert parse_usage_payload({}).remaining_ratio() is None
    assert parse_usage_payload({"five_hour": None, "seven_day": None}).remaining_ratio() is None
    assert parse_usage_payload({"five_hour": {"utilization": "46"}}).remaining_ratio() is None


def test_usage_payload_feeds_the_tracker_like_headers_do() -> None:
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    assert tracker.observe(parse_usage_payload({"five_hour": {"utilization": 95.0}})) is BackendMode.FALLBACK
    assert tracker.observe(parse_usage_payload({"five_hour": {"utilization": 70.0}})) is BackendMode.ANTHROPIC


def _ollama(session: float | None, weekly: float | None) -> OllamaSnapshot:
    return parse_ollama_usage(
        {"limits": {"session": {"usage": session}, "weekly": {"usage": weekly}}}
    )


def _anthropic(util_5h: float, util_7d: float) -> QuotaSnapshot:
    return parse_quota_headers(
        httpx.Headers(
            {
                "anthropic-ratelimit-unified-5h-utilization": str(util_5h),
                "anthropic-ratelimit-unified-7d-utilization": str(util_7d),
            }
        )
    )


def test_parse_ollama_usage_reads_ratios_and_model_counts() -> None:
    snapshot = parse_ollama_usage(
        {
            "limits": {
                "session": {"usage": 0, "models": [{"name": "glm-5.3-flash", "request_count": 6}]},
                "weekly": {
                    "usage": 0.822,
                    "models": [
                        {"name": "gemma4:31b", "request_count": 32},
                        {"name": "glm-5.3-flash", "request_count": 2672},
                    ],
                },
            }
        }
    )
    assert snapshot.session_utilization == pytest.approx(0.0)
    assert snapshot.weekly_utilization == pytest.approx(0.822)
    assert snapshot.weekly_remaining_ratio() == pytest.approx(0.178)
    # 週次窓の方が逼迫している / the weekly window is the tighter of the two
    assert snapshot.remaining_ratio() == pytest.approx(0.178)
    # 多い順に並ぶ / sorted by request count, descending
    assert snapshot.weekly_models[0] == ("glm-5.3-flash", 2672)


def test_parse_ollama_usage_tolerates_odd_shapes() -> None:
    assert parse_ollama_usage({}).remaining_ratio() is None
    assert parse_ollama_usage({"limits": {"weekly": {"usage": "0.8"}}}).remaining_ratio() is None
    assert parse_ollama_usage({"limits": None}).weekly_models == ()


def test_exhausted_ollama_is_never_used_as_a_fallback() -> None:
    """Failing over into an Ollama account that is out of quota helps nobody.

    quotaが尽きたOllamaへフェイルオーバーしても意味がないため、切り替えないこと。
    """
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    tracker.observe_ollama(_ollama(session=0.0, weekly=0.99))
    # Anthropicが残5%でも、Ollamaが残1%なら切り替えない
    # Even with Anthropic at 5% left, 1% left on Ollama is no improvement
    assert tracker.observe(_anthropic(0.95, 0.5)) is BackendMode.ANTHROPIC
    assert tracker.last_reason == "ollama_exhausted"


def test_below_floor_ollama_is_used_when_anthropic_is_dead() -> None:
    """Ollama under its floor is still the right answer once Anthropic reads zero.

    Anthropicの5時間窓が底を突き、週次窓だけ残っている状態。Ollamaの残量が
    ollama_min_remaining_pct(既定5%)を下回っていても、Anthropicが実質枯渇している
    以上、Ollamaの残りを使う方がまだましである。実際に起きた不具合の再現ケース。
    """
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    # 実障害時の実測値: Ollama残4.7%(min=閾値5%未満)、Anthropicは5h使い切りで残0
    # The real incident's numbers: Ollama at 4.7% (under the 5% floor),
    # Anthropic with its 5h window at exactly zero.
    tracker.observe_ollama(_ollama(session=0.741, weekly=0.953))
    assert tracker.observe(_anthropic(1.0, 0.822)) is BackendMode.FALLBACK
    assert tracker.last_reason == "anthropic_low"


def test_rate_limited_anthropic_fails_over_until_real_numbers_arrive() -> None:
    """A 429 counts as exhaustion even though it carries no headers.

    429には使用率ヘッダーが載らないため、これを記録しないと「データなし」のまま
    枯渇したAnthropicに留まり続ける。記録したらフォールバックし、実数値の観測が
    届いた時点でフラグは解けること。
    """
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    tracker.observe_ollama(_ollama(session=0.1, weekly=0.2))

    # Anthropicの観測が一切無くても、429は枯渇の証拠として扱う
    # A 429 is exhaustion evidence even with no Anthropic reading at all
    assert tracker.note_anthropic_rate_limited() is BackendMode.FALLBACK
    assert tracker.last_reason == "anthropic_rate_limited"

    # ヘッダー無しの観測(中継された429の解釈結果)ではフォールバックを維持する
    # A header-less snapshot says nothing, so fallback stands
    tracker.observe(parse_quota_headers(httpx.Headers()))
    assert tracker.mode is BackendMode.FALLBACK

    # 実数値が届いたらフラグは解け、通常のヒステリシスで切り戻る
    # Real numbers retire the flag, and ordinary hysteresis switches back
    assert tracker.observe(_anthropic(0.5, 0.2)) is BackendMode.ANTHROPIC
    assert tracker.last_reason == "anthropic_recovered"


def test_weekly_balance_prefers_the_side_with_more_weekly_left() -> None:
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        policy=RoutingPolicy.WEEKLY_BALANCE,
    )
    # 短い窓はどちらも余裕あり。週次はAnthropic残30% / Ollama残80% -> Ollamaへ
    # Both short windows are comfortable; weekly is 30% vs 80%, so Ollama wins
    tracker.observe_ollama(_ollama(session=0.1, weekly=0.2))
    assert tracker.observe(_anthropic(0.1, 0.7)) is BackendMode.FALLBACK
    assert tracker.last_reason == "weekly_balance"

    # Ollama週次が枯れてきたら戻る / once Ollama's weekly drains, come back
    tracker.observe_ollama(_ollama(session=0.1, weekly=0.9))
    assert tracker.mode is BackendMode.ANTHROPIC


def test_weekly_balance_stands_down_when_a_short_window_is_low() -> None:
    """Balancing is for spreading the weekly budget, not for rescuing a tight window.

    バランシングは週次予算を均すためのもので、逼迫した短い窓の救済には使わない。
    """
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        policy=RoutingPolicy.WEEKLY_BALANCE,
    )
    # Ollamaのセッション窓が残20%(floor 50%未満)なので週次比較は行わない。
    # Anthropic側はどの窓も余裕がある値にして、ハードガードと切り分ける。
    # Ollama's session window is at 20% left, below the 50% floor, so no balancing.
    # Anthropic is kept comfortable so this cannot be confused with the hard guard.
    tracker.observe_ollama(_ollama(session=0.8, weekly=0.0))
    assert tracker.observe(_anthropic(0.1, 0.5)) is BackendMode.ANTHROPIC


def test_weekly_balance_needs_a_margin_before_switching() -> None:
    """A near-tie must not make the backend oscillate.

    拮抗している場合に切り替え続けない(振動防止)こと。
    """
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        policy=RoutingPolicy.WEEKLY_BALANCE,
        balance_margin_pct=10.0,
    )
    # 週次はAnthropic残50% / Ollama残55%。差5%はマージン10%未満なので動かない
    # Weekly: 50% vs 55% left. The 5-point gap is under the 10-point margin.
    tracker.observe_ollama(_ollama(session=0.1, weekly=0.45))
    assert tracker.observe(_anthropic(0.1, 0.5)) is BackendMode.ANTHROPIC


def test_anthropic_low_overrides_the_balancing_policy() -> None:
    """The hard guard still wins: a critical Anthropic window always fails over.

    ハードガードは常に優先される。Anthropicが逼迫したら必ずフェイルオーバーする。
    """
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        policy=RoutingPolicy.WEEKLY_BALANCE,
    )
    tracker.observe_ollama(_ollama(session=0.1, weekly=0.1))
    assert tracker.observe(_anthropic(0.95, 0.1)) is BackendMode.FALLBACK
    assert tracker.last_reason == "anthropic_low"


def test_reverse_failover_returns_to_anthropic_after_repeated_ollama_failures() -> None:
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        ollama_failure_threshold=5,
        reverse_failover_min_5h_pct=10.0,
    )
    tracker.observe_ollama(_ollama(session=0.1, weekly=0.1))
    tracker.observe(_anthropic(0.95, 0.1))
    assert tracker.mode is BackendMode.FALLBACK

    # 5時間窓は残50%まで戻ったが、週次窓が残15%で回復閾値20%に届かないためFALLBACKのまま。
    # 逆フェイルオーバーが「回復による切り戻し」と紛れないようにするための状態。
    # The 5h window recovered to 50% left, but the weekly window sits at 15% -
    # under the 20% recovery threshold - so it stays in fallback. This keeps the
    # reverse failover distinguishable from an ordinary recovery.
    tracker.observe(_anthropic(0.5, 0.85))
    assert tracker.mode is BackendMode.FALLBACK

    # 4回では動かない / four failures are not enough
    assert tracker.note_ollama_failures(4) is BackendMode.FALLBACK
    # 5回目で戻る / the fifth failure triggers it
    assert tracker.note_ollama_failures(5) is BackendMode.ANTHROPIC
    assert tracker.last_reason == "ollama_failures"

    # クールダウン中はAnthropicが逼迫しても戻らない / no going back during the cooldown
    assert tracker.observe(_anthropic(0.99, 0.1)) is BackendMode.ANTHROPIC
    assert tracker.last_reason == "ollama_cooldown"


def test_reverse_failover_stays_put_when_anthropic_has_no_room() -> None:
    """With Anthropic also out of room there is nowhere better to go.

    Anthropic側にも余裕が無いなら、逃げ先が無いので動かないこと。
    """
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        ollama_failure_threshold=5,
        reverse_failover_min_5h_pct=10.0,
    )
    tracker.observe_ollama(_ollama(session=0.1, weekly=0.1))
    tracker.observe(_anthropic(0.95, 0.1))
    assert tracker.mode is BackendMode.FALLBACK
    assert tracker.note_ollama_failures(10) is BackendMode.FALLBACK


def _anthropic_with_resets(util_5h: float, reset_5h: float, util_7d: float, reset_7h: float) -> QuotaSnapshot:
    s = parse_quota_headers(
        httpx.Headers(
            {
                "anthropic-ratelimit-unified-5h-utilization": str(util_5h),
                "anthropic-ratelimit-unified-7d-utilization": str(util_7d),
            }
        )
    )
    # parse_timestamp 経由ではなく直接代入する(テストで時刻を固定するため)。
    # Set directly rather than through parse_timestamp, to freeze the clock.
    return QuotaSnapshot(
        utilization_5h=s.utilization_5h,
        utilization_7d=s.utilization_7d,
        requests_remaining_ratio=None,
        tokens_remaining_ratio=None,
        observed_at=time.time(),
        reset_5h=reset_5h,
        reset_7d=reset_7h,
        source=SOURCE_HEADERS,
    )


def test_burn_rate_switches_to_ollama_when_anthropic_is_burning_faster() -> None:
    """ユーザーが挙げた数値例そのもの: Anthropic残70%/4h後リセット vs Ollama残100%。

    Anthropicのバーンレート0.875(重み1.1で0.9625) < Ollamaの1.0 なのでOllamaへ。
    """
    now = time.time()
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        policy=RoutingPolicy.BURN_RATE_BALANCE,
        anthropic_priority_weight=1.1,
    )
    tracker.observe_ollama(_ollama(session=0.0, weekly=0.0))
    # Ollamaの週次は残100%(=1.0)。リセット時刻不明なので分母は最大値。
    tracker.observe(_anthropic_with_resets(0.30, now + 4 * 3600, 0.1, now + 6 * 86400))
    assert tracker.mode is BackendMode.FALLBACK
    assert tracker.last_reason == "burn_rate_balance"


def test_burn_rate_stays_with_weighted_anthropic_at_parity() -> None:
    """重み1.1により、Ollamaがわずかに余裕があるだけでは移らないこと。

    Anthropic残50%/2.5h後リセット=1.0、重み1.1で1.1。Ollama残100%=1.0。
    Anthropicの方が上なので動かない。
    """
    now = time.time()
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        policy=RoutingPolicy.BURN_RATE_BALANCE,
        anthropic_priority_weight=1.1,
    )
    tracker.observe_ollama(_ollama(session=0.0, weekly=0.0))
    tracker.observe(_anthropic_with_resets(0.5, now + 2.5 * 3600, 0.1, now + 6 * 86400))
    assert tracker.mode is BackendMode.ANTHROPIC


def test_burn_rate_ignores_unknown_reset_times_rather_than_panic() -> None:
    """Anthropic側もリセット時刻が不明なら、Ollamaと同じく最大見積もりになる。

    残量比較に等化する(窓の時間正規化が効かない)ため、Ollama残100% vs Anthropic残99%
    のような極端な差がなければ、重み1.1の分Anthropicが優先される。
    """
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        policy=RoutingPolicy.BURN_RATE_BALANCE,
        anthropic_priority_weight=1.1,
    )
    tracker.observe_ollama(_ollama(session=0.0, weekly=0.0))
    # リセット時刻無し(=分母1.0固定)。両窓の最小残量は99%。
    # Anthropic: 0.99 × 1.1 = 1.089 > Ollama: 1.0
    tracker.observe(_anthropic_with_resets(0.01, None, 0.01, None))
    assert tracker.mode is BackendMode.ANTHROPIC


def test_burn_rate_never_engages_when_a_session_window_is_low() -> None:
    """short窓が逼迫している場合はバランスを試みず、通常の切替ロジックに委ねる。

    Ollama残40%(floor 50%未満)ではバーンレート比較自体を行わない。
    """
    now = time.time()
    tracker = QuotaTracker(
        fallback_threshold_pct=10.0,
        recovery_threshold_pct=20.0,
        policy=RoutingPolicy.BURN_RATE_BALANCE,
    )
    tracker.observe_ollama(_ollama(session=0.6, weekly=0.0))
    tracker.observe(_anthropic_with_resets(0.3, now + 4 * 3600, 0.1, now + 6 * 86400))
    assert tracker.mode is BackendMode.ANTHROPIC
    assert tracker.last_reason != "burn_rate_balance"


def test_overdrawn_utilization_is_clamped_to_exhausted() -> None:
    """Anthropic reports >1.0 utilization when a window is overdrawn; read it as empty.

    The TUI showed "-12% remaining" for exactly this input before the clamp.

    窓が枠超過するとAnthropicは1.0超の利用率を返す。クランプ前のTUIはまさに
    この入力に対して「残量-12%」を表示していた。
    """
    snapshot = parse_quota_headers(
        httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "1.12"})
    )
    assert snapshot.utilization_5h == pytest.approx(1.0)
    assert snapshot.remaining_ratio() == pytest.approx(0.0)

    # 逆側(負の利用率)も同じく0残量扱い / a negative reading reads as empty too
    snapshot = parse_quota_headers(
        httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "-0.05"})
    )
    assert snapshot.utilization_5h == pytest.approx(0.0)


def test_overdrawn_remaining_ratio_is_clamped() -> None:
    """API-key billing: remaining can go negative once the limit is hit.

    APIキー課金でも、枠に達した後は remaining が負になりうる。
    """
    snapshot = parse_quota_headers(
        httpx.Headers(
            {
                "anthropic-ratelimit-requests-limit": "1000",
                "anthropic-ratelimit-requests-remaining": "-50",
            }
        )
    )
    assert snapshot.requests_remaining_ratio == pytest.approx(0.0)


def test_overdrawn_anthropic_fails_over_immediately() -> None:
    """A clamped 0-remaining reading must route to Ollama with no hysteresis.

    The threshold logic already handles this once the value stops going
    negative, so no special case is needed: 0 < 10% is a failover.

    枠超過を残量0として読めば、閾値判定がそのまま働いて即座にフェイルオーバー
    する。特別扱いは不要(0 < 10% は切替条件)。ヒステリシスの逆側(復帰)は
    残量0では発動しないため、しつこくOllamaを使い続けることにもならない。
    """
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    assert tracker.observe(
        parse_quota_headers(httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "1.12"}))
    ) is BackendMode.FALLBACK

    # 枠超過のままでは回復判定も発動しない / no recovery while still overdrawn
    assert tracker.observe(
        parse_quota_headers(httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "1.05"}))
    ) is BackendMode.FALLBACK


def test_reset_estimator_bounds_from_utilization_rises() -> None:
    """利用率の上昇を窓の始点とみなし、「始点＋窓長」を上限として返すこと。

    A rise marks a fresh window, so the estimate is rise time + window length -
    the latest the reset could have been, never the truth.
    """
    estimator = OllamaResetEstimator()

    # 1回目: 上昇が無いため予測も立たない / first reading, no rise, no estimate
    s1 = estimator.update(parse_ollama_usage({"limits": {"session": {"usage": 0.2}}}))
    assert s1.estimated_session_reset is None

    # 2回目: 上昇 → 上限値を予測 / a rise, so the bound is now + window length
    t2 = time.time()
    s2 = estimator.update(
        OllamaSnapshot(session_utilization=0.5, weekly_utilization=None, observed_at=t2)
    )
    assert s2.estimated_session_reset == pytest.approx(
        t2 + OLLAMA_SESSION_WINDOW_SECONDS
    )

    # 3回目: さらに上昇 → 上限値が更新される / another rise re-bounds it
    t3 = t2 + 3600
    s3 = estimator.update(
        OllamaSnapshot(session_utilization=0.7, weekly_utilization=None, observed_at=t3)
    )
    assert s3.estimated_session_reset == pytest.approx(
        t3 + OLLAMA_SESSION_WINDOW_SECONDS
    )


def test_reset_estimator_ignores_falling_or_equal_utilization() -> None:
    """利用率が下がるのは通常の利用の帰結であり、窓の切替を意味しないこと。

    A fall is what ordinary usage does; only a rise can mark a fresh window.
    """
    estimator = OllamaResetEstimator()
    s1 = estimator.update(parse_ollama_usage({"limits": {"session": {"usage": 0.5}}}))
    assert s1.estimated_session_reset is None

    # 下がっても予測は立たない / a fall yields no estimate
    s2 = estimator.update(
        OllamaSnapshot(session_utilization=0.3, weekly_utilization=None, observed_at=time.time())
    )
    assert s2.estimated_session_reset is None


def test_reset_estimator_handles_weekly_window_independently() -> None:
    """セッション窓と週次窓が独立に予測されること。

    The two windows rise at different times and are tracked separately.
    """
    estimator = OllamaResetEstimator()
    t0 = time.time()
    # 初回は予測なし / the first reading yields no estimates
    s0 = estimator.update(parse_ollama_usage({"limits": {"session": {"usage": 0.1}, "weekly": {"usage": 0.1}}}))
    assert s0.estimated_session_reset is None
    assert s0.estimated_weekly_reset is None

    # 週次だけ上昇 / only the weekly window rises
    s1 = estimator.update(
        OllamaSnapshot(session_utilization=0.1, weekly_utilization=0.3, observed_at=t0)
    )
    assert s1.estimated_session_reset is None
    assert s1.estimated_weekly_reset == pytest.approx(t0 + OLLAMA_WEEKLY_WINDOW_SECONDS)

    # その後セッション窓が上昇しても、週次の予測値は変わらない
    # A later session rise must not disturb the weekly estimate
    t1 = t0 + 7200
    s2 = estimator.update(
        OllamaSnapshot(session_utilization=0.4, weekly_utilization=0.3, observed_at=t1)
    )
    assert s2.estimated_session_reset == pytest.approx(t1 + OLLAMA_SESSION_WINDOW_SECONDS)
    assert s2.estimated_weekly_reset == pytest.approx(t0 + OLLAMA_WEEKLY_WINDOW_SECONDS)


def test_reset_estimator_survives_a_none_reading() -> None:
    """欠損値を挟んでも予測が壊れないこと。

    A None reading in between must not corrupt the tracker.
    """
    estimator = OllamaResetEstimator()
    t0 = time.time()
    estimator.update(OllamaSnapshot(session_utilization=0.2, weekly_utilization=None, observed_at=t0))
    # 欠損 / a missing reading
    estimator.update(OllamaSnapshot(session_utilization=None, weekly_utilization=None, observed_at=t0 + 60))
    # 欠損を挟んだ上で上昇 / a rise after the gap
    s = estimator.update(
        OllamaSnapshot(session_utilization=0.6, weekly_utilization=None, observed_at=t0 + 120)
    )
    assert s.estimated_session_reset == pytest.approx(
        t0 + 120 + OLLAMA_SESSION_WINDOW_SECONDS
    )
