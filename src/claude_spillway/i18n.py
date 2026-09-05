"""Locale-aware message catalog for user-facing text.

The proxy is meant to be usable outside Japan as well, so every string that
is shown to a human defaults to English and is rendered in Japanese only
when the environment locale asks for it. Log records emitted through
``logging`` are intentionally left in English so that they stay greppable.

海外ユーザーの利用も想定しているため、ユーザーに見えるメッセージは
既定を英語とし、環境のロケールが日本語のときだけ日本語で表示する。
``logging`` 経由のログはgrepしやすさを優先して常に英語のままとする。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Explicit override that wins over every LC_*/LANG variable.
#: LC_*/LANG より優先される明示的な上書き用の環境変数。
LANGUAGE_ENV_OVERRIDE = "CLAUDE_SPILLWAY_LANG"

#: Locale environment variables, in the order they are consulted.
#: 参照するロケール環境変数(先に見つかったものを採用する)。
_LOCALE_ENV_NAMES = ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE")

#: Languages this catalog knows about. Anything else falls back to English.
#: このカタログが対応する言語。それ以外はすべて英語にフォールバックする。
_SUPPORTED_LANGUAGES = ("en", "ja")

_DEFAULT_LANGUAGE = "en"

#: Message catalog: key -> {language code -> text}.
#: Values are formatted with :meth:`str.format`, so ``{name}`` placeholders
#: are filled from the keyword arguments passed to :func:`t`.
#: Note: entries used as argparse ``help=`` must escape a literal percent
#: sign as ``%%`` because argparse applies %-formatting to help strings.
#:
#: メッセージカタログ: キー -> {言語コード -> 文言}。
#: 値は :meth:`str.format` で整形するため、``{name}`` は :func:`t` に渡した
#: キーワード引数で置換される。
#: 注意: argparse の ``help=`` に渡す項目は、argparse が%書式を適用する
#: 関係でリテラルの%を ``%%`` とエスケープしておく必要がある。
_MESSAGES: dict[str, dict[str, str]] = {
    # --- CLI ---------------------------------------------------------------
    "cli.description": {
        "en": (
            "Quota-aware failover proxy for Claude Code. "
            "When your Anthropic usage window (5h / weekly) runs low it automatically "
            "spills over to Ollama Cloud, and switches back once quota recovers."
        ),
        "ja": (
            "Claude Code用のquota連動フェイルオーバープロキシ。"
            "Anthropicの利用枠(5時間窓/週次窓)が逼迫すると自動的にOllama Cloudへ"
            "切り替え、回復したらAnthropicへ戻します。"
        ),
    },
    "cli.serve.help": {
        "en": "Start the proxy server",
        "ja": "プロキシサーバーを起動する",
    },
    "cli.serve.config": {
        "en": (
            "Path to the YAML config file "
            "(default: $CLAUDE_SPILLWAY_CONFIG, then the per-user config directory)"
        ),
        "ja": (
            "設定YAMLファイルへのパス"
            "(省略時: $CLAUDE_SPILLWAY_CONFIG → ユーザー設定ディレクトリ の順に探索)"
        ),
    },
    "cli.config.none": {
        "en": "(not found; using built-in defaults)",
        "ja": "(見つからないため組み込みのデフォルト値を使用)",
    },
    "cli.warn.config_missing": {
        "en": "warning: config file not found: {path} (continuing with built-in defaults)",
        "ja": "warning: 設定ファイルが見つかりません: {path} (組み込みのデフォルト値で続行します)",
    },
    "cli.serve.host": {
        "en": "Listen host (overrides the config file)",
        "ja": "待受ホスト(設定ファイルを上書き)",
    },
    "cli.serve.port": {
        "en": "Listen port (overrides the config file)",
        "ja": "待受ポート(設定ファイルを上書き)",
    },
    "cli.serve.fallback_threshold": {
        "en": "Fail over to Ollama once the remaining quota drops below this %% (default: 10)",
        "ja": "残量がこの%%を下回るとOllamaへフェイルオーバーする(デフォルト: 10)",
    },
    "cli.serve.recovery_threshold": {
        "en": "Switch back to Anthropic once the remaining quota recovers to this %% (default: 20)",
        "ja": "残量がこの%%まで回復するとAnthropicへ戻す(デフォルト: 20)",
    },
    "cli.serve.log_level": {
        "en": "Log level (DEBUG/INFO/WARNING/ERROR)",
        "ja": "ログレベル(DEBUG/INFO/WARNING/ERROR)",
    },
    "cli.monitor.help": {
        "en": "Watch a running proxy's status in a TUI",
        "ja": "稼働中プロキシの状態をTUIで監視する",
    },
    "cli.monitor.host": {
        "en": "Host of the proxy to watch",
        "ja": "監視対象プロキシのホスト",
    },
    "cli.monitor.port": {
        "en": "Port of the proxy to watch",
        "ja": "監視対象プロキシのポート",
    },
    "cli.monitor.interval": {
        "en": "Polling interval in seconds",
        "ja": "ポーリング間隔(秒)",
    },
    "cli.warn.no_ollama_key": {
        "en": "warning: ollama.api_key is not set; requests will fail once we fail over.",
        "ja": "warning: ollama.api_key が未設定です。フェイルオーバー時のリクエストは失敗します。",
    },
    # --- monitor TUI -------------------------------------------------------
    "monitor.error.title": {
        "en": "Connection error",
        "ja": "接続エラー",
    },
    "monitor.error.body": {
        "en": "cannot connect to {url}: {error}",
        "ja": "{url} に接続できません: {error}",
    },
    "monitor.waiting.title": {
        "en": "Waiting",
        "ja": "待機中",
    },
    "monitor.waiting.body": {
        "en": (
            "No request has gone through this proxy yet.\n"
            "Point ANTHROPIC_BASE_URL at this proxy and send any request\n"
            "from Claude Code, and the quota status will show up here."
        ),
        "ja": (
            "まだこのプロキシを経由したリクエストが観測されていません。\n"
            "ANTHROPIC_BASE_URL をこのプロキシに向けて Claude Code から\n"
            "何かリクエストを送ると、ここにquota状況が表示されます。"
        ),
    },
    "monitor.quota.title": {
        "en": "Anthropic quota",
        "ja": "Anthropic quota",
    },
    "monitor.col.metric": {"en": "metric", "ja": "指標"},
    "monitor.col.remaining": {"en": "remaining", "ja": "残量"},
    "monitor.col.observed_at": {"en": "observed at", "ja": "観測時刻"},
    "monitor.row.window_5h": {"en": "5h window", "ja": "5時間窓"},
    "monitor.row.window_7d": {"en": "weekly window", "ja": "週次窓"},
    "monitor.row.requests": {
        "en": "requests (API-key billing)",
        "ja": "リクエスト数(APIキー課金時)",
    },
    "monitor.row.tokens": {
        "en": "tokens (API-key billing)",
        "ja": "トークン数(APIキー課金時)",
    },
    "monitor.row.min_remaining": {
        "en": "min. remaining (used for switching)",
        "ja": "判定に使う最小残量",
    },
    "monitor.ollama.title": {
        "en": "Ollama Cloud (self-measured; no official quota API exists)",
        "ja": "Ollama Cloud (自己計測値。公式quota APIは存在しません)",
    },
    "monitor.col.item": {"en": "item", "ja": "項目"},
    "monitor.col.value": {"en": "value", "ja": "値"},
    "monitor.row.relayed": {"en": "requests relayed", "ja": "中継リクエスト数"},
    "monitor.row.failed": {"en": "failures", "ja": "失敗数"},
    "monitor.row.last_status": {"en": "last status code", "ja": "直近ステータスコード"},
    "monitor.row.last_request_at": {"en": "last request at", "ja": "直近リクエスト時刻"},
    "monitor.row.last_error": {"en": "last error", "ja": "直近エラー"},
    # Rendered by rich, not argparse, so a literal percent stays single here.
    # ここは argparse ではなく rich で描画するため、%はエスケープ不要。
    "monitor.footer.thresholds": {
        "en": (
            "fallback: switch over below {fallback}% remaining / "
            "recovery: switch back at {recovery}% remaining or above"
        ),
        "ja": ("fallback閾値: 残り{fallback}% 未満で切替 / recovery閾値: 残り{recovery}%以上で復帰"),
    },
}

#: Cached result of :func:`detect_language`. ``None`` means "not resolved yet".
#: :func:`detect_language` の結果キャッシュ。``None`` は未解決を意味する。
_language: str | None = None


def _normalize(raw: str) -> str | None:
    """Map a raw locale string to a supported language code, or ``None``.

    ``ja``, ``ja_JP``, ``ja_JP.UTF-8`` and ``ja:en`` all resolve to Japanese.
    ``C`` / ``POSIX`` explicitly mean "no localization", i.e. English.

    ``ja`` / ``ja_JP`` / ``ja_JP.UTF-8`` / ``ja:en`` はいずれも日本語と判定する。
    ``C`` / ``POSIX`` は「ローカライズしない」の意なので英語と判定する。
    """
    value = raw.strip().lower()
    if not value:
        return None
    if value in ("c", "posix"):
        return _DEFAULT_LANGUAGE
    # LANGUAGE may hold a colon-separated priority list such as "ja:en".
    # LANGUAGE は "ja:en" のようなコロン区切りの優先順リストになりうる。
    for candidate in value.split(":"):
        code = candidate.split(".")[0].split("_")[0].split("-")[0]
        if code in _SUPPORTED_LANGUAGES:
            return code
    return None


def detect_language(env: Mapping[str, str] | None = None) -> str:
    """Resolve the display language from environment variables.

    環境変数から表示言語を決定する。

    :param env: Mapping to read instead of :data:`os.environ` (for tests).
        :data:`os.environ` の代わりに参照するマッピング(テスト用)。
    :return: ``"ja"`` or ``"en"``. / ``"ja"`` または ``"en"``。
    """
    source = os.environ if env is None else env
    override = source.get(LANGUAGE_ENV_OVERRIDE)
    if override:
        # An explicit override is authoritative even if it is unknown; an
        # unknown value simply falls back to English.
        # 明示指定は最優先。未知の値だった場合は素直に英語へフォールバックする。
        return _normalize(override) or _DEFAULT_LANGUAGE
    for name in _LOCALE_ENV_NAMES:
        value = source.get(name)
        if not value:
            continue
        language = _normalize(value)
        if language is not None:
            return language
    return _DEFAULT_LANGUAGE


def get_language() -> str:
    """Return the current display language, detecting it once and caching it.

    現在の表示言語を返す(初回のみ判定し、以降はキャッシュを使う)。
    """
    global _language
    if _language is None:
        _language = detect_language()
    return _language


def set_language(language: str | None) -> None:
    """Force the display language, or pass ``None`` to re-detect it.

    表示言語を強制する。``None`` を渡すと次回参照時に再判定する。
    """
    global _language
    if language is None:
        _language = None
    else:
        _language = language if language in _SUPPORTED_LANGUAGES else _DEFAULT_LANGUAGE


def t(key: str, **kwargs: object) -> str:
    """Look up ``key`` in the catalog and format it with ``kwargs``.

    Falls back to English when the current language has no entry, and to the
    key itself when the key is unknown, so a missing translation can never
    crash the CLI.

    カタログから ``key`` を引き、``kwargs`` で整形して返す。
    現在の言語にエントリが無ければ英語に、キー自体が未知ならキー文字列に
    フォールバックするため、翻訳漏れでCLIが落ちることはない。
    """
    entry = _MESSAGES.get(key)
    if entry is None:
        return key
    template = entry.get(get_language()) or entry.get(_DEFAULT_LANGUAGE, key)
    if not kwargs:
        return template
    return template.format(**kwargs)
