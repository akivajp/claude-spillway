"""monitor.py の描画ロジックの単体テスト。"""

from __future__ import annotations

from rich.console import Console

from claude_spillway.monitor import _build_renderable


def _render_to_text(data: dict, url: str = "http://127.0.0.1:8787/_spillway/status", error: str | None = None) -> str:
    console = Console(record=True, width=100)
    console.print(_build_renderable(data, url, error))
    return console.export_text()


def test_shows_waiting_message_before_any_traffic() -> None:
    data = {
        "mode": "anthropic",
        "anthropic": {"observed_at": None},
        "ollama": {"last_request_at": None},
        "thresholds": {"fallback_pct": 10.0, "recovery_pct": 20.0},
    }
    text = _render_to_text(data)
    assert "待機中" in text
    assert "Anthropic quota" not in text


def test_shows_quota_table_once_traffic_observed() -> None:
    data = {
        "mode": "anthropic",
        "anthropic": {
            "utilization_5h": 0.3,
            "utilization_7d": 0.1,
            "requests_remaining_ratio": None,
            "tokens_remaining_ratio": None,
            "remaining_ratio": 0.7,
            "observed_at": 1700000000.0,
        },
        "ollama": {
            "requests_sent": 0,
            "requests_failed": 0,
            "last_status_code": None,
            "last_request_at": None,
            "last_error": None,
        },
        "thresholds": {"fallback_pct": 10.0, "recovery_pct": 20.0},
    }
    text = _render_to_text(data)
    assert "待機中" not in text
    assert "Anthropic quota" in text


def test_connection_error_shown() -> None:
    text = _render_to_text({}, error="connection refused")
    assert "接続エラー" in text
    assert "connection refused" in text
