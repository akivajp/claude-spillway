"""Unit tests for the rendering logic in monitor.py.

monitor.py の描画ロジックの単体テスト。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from rich.console import Console

from claude_spillway import i18n
from claude_spillway.monitor import _build_renderable


@pytest.fixture(autouse=True)
def _reset_language() -> Iterator[None]:
    """Keep the forced language from leaking between tests.

    テスト間で強制した表示言語が漏れないようにリセットする。
    """
    yield
    i18n.set_language(None)


_STATUS_URL = "http://127.0.0.1:8787/_spillway/status"
_DASHBOARD_URL = "http://127.0.0.1:8787/_spillway/"


def _render_to_text(data: dict, url: str = _STATUS_URL, error: str | None = None) -> str:
    console = Console(record=True, width=100)
    console.print(_build_renderable(data, url, _DASHBOARD_URL, error))
    return console.export_text()


_WAITING_DATA = {
    "mode": "anthropic",
    "anthropic": {"observed_at": None},
    "ollama": {"last_request_at": None},
    "thresholds": {"fallback_pct": 10.0, "recovery_pct": 20.0},
}

_OBSERVED_DATA = {
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


# The display language is forced explicitly so the assertions do not depend on
# the locale of the machine running the tests.
# テスト実行機のロケールに依存しないよう、表示言語を明示的に固定する。
@pytest.mark.parametrize(("language", "waiting_title"), [("en", "Waiting"), ("ja", "待機中")])
def test_shows_waiting_message_before_any_traffic(language: str, waiting_title: str) -> None:
    i18n.set_language(language)
    text = _render_to_text(_WAITING_DATA)
    assert waiting_title in text
    assert "Anthropic quota" not in text


@pytest.mark.parametrize(("language", "metric_header"), [("en", "metric"), ("ja", "指標")])
def test_shows_quota_table_once_traffic_observed(language: str, metric_header: str) -> None:
    i18n.set_language(language)
    text = _render_to_text(_OBSERVED_DATA)
    assert "Anthropic quota" in text
    assert metric_header in text


@pytest.mark.parametrize(("language", "error_title"), [("en", "Connection error"), ("ja", "接続エラー")])
def test_connection_error_shown(language: str, error_title: str) -> None:
    i18n.set_language(language)
    text = _render_to_text({}, error="connection refused")
    assert error_title in text
    assert "connection refused" in text


def test_defaults_to_english_outside_japanese_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-Japanese (or unset) locale must render the English text.

    日本語以外(あるいは未設定)のロケールでは英語表示になること。
    """
    for name in (i18n.LANGUAGE_ENV_OVERRIDE, "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    i18n.set_language(None)
    text = _render_to_text(_WAITING_DATA)
    assert "Waiting" in text
    assert "待機中" not in text


@pytest.mark.parametrize("data", [_WAITING_DATA, _OBSERVED_DATA])
def test_header_always_shows_the_dashboard_url(data: dict) -> None:
    """The browser view is only discoverable from here, so it must always show.

    The header is the one place that renders in every state, including the
    waiting one - which is exactly when someone is most likely to go looking
    for another way to see what is going on.

    ブラウザ表示への入口はここにしかないため、常に表示されること。
    ヘッダーは待機中を含むすべての状態で描画される唯一の箇所であり、
    別の見方を探したくなるのはまさに待機中だからである。
    """
    assert _DASHBOARD_URL in _render_to_text(data)


def test_header_shows_the_dashboard_url_while_unreachable() -> None:
    """Even with the proxy down, the URL stays visible for when it comes back.

    プロキシが落ちている間も、復帰後に使えるようURLは出したままにする。
    """
    assert _DASHBOARD_URL in _render_to_text({}, error="connection refused")
