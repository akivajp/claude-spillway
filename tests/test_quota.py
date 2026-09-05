"""Unit tests for quota.py. / quota.py の単体テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from claude_spillway.quota import (
    SOURCE_HEADERS,
    SOURCE_USAGE_API,
    BackendMode,
    QuotaTracker,
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
