"""Unit tests for the locale detection and message catalog.

ロケール判定とメッセージカタログの単体テスト。
"""

from __future__ import annotations

import pytest

from claude_spillway import i18n


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        # Japanese locales in their common spellings.
        # よくある表記の日本語ロケール。
        ({"LANG": "ja_JP.UTF-8"}, "ja"),
        ({"LANG": "ja"}, "ja"),
        ({"LC_ALL": "ja_JP.utf8"}, "ja"),
        ({"LANGUAGE": "ja:en"}, "ja"),
        # Anything else — including unset, C/POSIX and unknown languages —
        # falls back to English.
        # それ以外(未設定・C/POSIX・未対応言語)はすべて英語にフォールバックする。
        ({}, "en"),
        ({"LANG": ""}, "en"),
        ({"LANG": "en_US.UTF-8"}, "en"),
        ({"LANG": "C"}, "en"),
        ({"LC_ALL": "POSIX"}, "en"),
        ({"LANG": "fr_FR.UTF-8"}, "en"),
        # LC_ALL wins over LANG, matching the usual POSIX precedence.
        # POSIXの慣例どおり LC_ALL が LANG より優先される。
        ({"LC_ALL": "ja_JP.UTF-8", "LANG": "en_US.UTF-8"}, "ja"),
        ({"LC_ALL": "en_US.UTF-8", "LANG": "ja_JP.UTF-8"}, "en"),
        # The explicit override beats every locale variable.
        # 明示的な上書きは、どのロケール変数よりも優先される。
        ({i18n.LANGUAGE_ENV_OVERRIDE: "ja", "LANG": "en_US.UTF-8"}, "ja"),
        ({i18n.LANGUAGE_ENV_OVERRIDE: "en", "LANG": "ja_JP.UTF-8"}, "en"),
        ({i18n.LANGUAGE_ENV_OVERRIDE: "klingon", "LANG": "ja_JP.UTF-8"}, "en"),
    ],
)
def test_detect_language(env: dict[str, str], expected: str) -> None:
    assert i18n.detect_language(env) == expected


def test_translation_switches_with_language() -> None:
    try:
        i18n.set_language("en")
        assert i18n.t("monitor.waiting.title") == "Waiting"
        i18n.set_language("ja")
        assert i18n.t("monitor.waiting.title") == "待機中"
    finally:
        i18n.set_language(None)


def test_placeholders_are_formatted() -> None:
    try:
        i18n.set_language("en")
        message = i18n.t("monitor.error.body", url="http://x", error="refused")
        assert "http://x" in message
        assert "refused" in message
    finally:
        i18n.set_language(None)


def test_unknown_key_returns_the_key_itself() -> None:
    """A missing translation must never crash the CLI.

    翻訳漏れがあってもCLIを落とさないこと。
    """
    assert i18n.t("no.such.key") == "no.such.key"
