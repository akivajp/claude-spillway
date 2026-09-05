"""Unit tests for quota.py. / quota.py の単体テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from claude_spillway.quota import (
    SOURCE_HEADERS,
    SOURCE_USAGE_API,
    BackendMode,
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
