"""quota.py の単体テスト。"""

from __future__ import annotations

import httpx
import pytest

from claude_spillway.quota import BackendMode, QuotaTracker, parse_quota_headers


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

    # 残り15% まで戻ったが、recovery閾値(残り20%)にはまだ届いていないので切り戻らない
    tracker.observe(parse_quota_headers(httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "0.85"})))
    assert tracker.mode is BackendMode.FALLBACK

    # 残り25% まで回復したので切り戻る
    tracker.observe(parse_quota_headers(httpx.Headers({"anthropic-ratelimit-unified-5h-utilization": "0.75"})))
    assert tracker.mode is BackendMode.ANTHROPIC


def test_tracker_ignores_snapshot_without_data() -> None:
    tracker = QuotaTracker(fallback_threshold_pct=10.0, recovery_threshold_pct=20.0)
    mode = tracker.observe(parse_quota_headers(httpx.Headers({})))
    assert mode is BackendMode.ANTHROPIC
