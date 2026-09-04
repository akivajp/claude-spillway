"""``claude-spillway`` コマンドラインエントリーポイント。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from .config import Settings

console = Console()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-spillway",
        description=(
            "Claude Code用のquota連動フェイルオーバープロキシ。"
            "Anthropicの利用枠(5時間窓/週次窓)が逼迫すると自動的にOllama Cloudへ"
            "切り替え、回復したらAnthropicへ戻します。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="プロキシサーバーを起動する")
    serve.add_argument("-c", "--config", type=Path, default=None, help="設定YAMLファイルへのパス")
    serve.add_argument("--host", type=str, default=None, help="待受ホスト(設定ファイルを上書き)")
    serve.add_argument("--port", type=int, default=None, help="待受ポート(設定ファイルを上書き)")
    serve.add_argument(
        "--fallback-threshold-pct",
        type=float,
        default=None,
        help="残量がこの%%を下回るとOllamaへフェイルオーバーする(デフォルト: 10)",
    )
    serve.add_argument(
        "--recovery-threshold-pct",
        type=float,
        default=None,
        help="残量がこの%%まで回復するとAnthropicへ戻す(デフォルト: 20)",
    )
    serve.add_argument("--log-level", type=str, default=None, help="ログレベル(DEBUG/INFO/WARNING/ERROR)")

    monitor = subparsers.add_parser("monitor", help="稼働中プロキシの状態をTUIで監視する")
    monitor.add_argument("--host", type=str, default="127.0.0.1", help="監視対象プロキシのホスト")
    monitor.add_argument("--port", type=int, default=8787, help="監視対象プロキシのポート")
    monitor.add_argument("--interval", type=float, default=1.0, help="ポーリング間隔(秒)")

    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    data = settings.model_dump()
    if args.host is not None:
        data["listen"]["host"] = args.host
    if args.port is not None:
        data["listen"]["port"] = args.port
    if args.fallback_threshold_pct is not None:
        data["quota"]["fallback_threshold_pct"] = args.fallback_threshold_pct
    if args.recovery_threshold_pct is not None:
        data["quota"]["recovery_threshold_pct"] = args.recovery_threshold_pct
    if args.log_level is not None:
        data["log_level"] = args.log_level
    return Settings.model_validate(data)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(name)s: %(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def _run_serve(args: argparse.Namespace) -> None:
    settings = Settings.load(args.config)
    settings = _apply_overrides(settings, args)
    _setup_logging(settings.log_level)

    console.rule("[bold cyan]claude-spillway[/bold cyan]")
    console.print(f"listen         : {settings.listen.host}:{settings.listen.port}")
    console.print(f"anthropic      : {settings.anthropic.base_url}")
    console.print(f"ollama         : {settings.ollama.base_url}")
    console.print(
        f"thresholds     : fallback<{settings.quota.fallback_threshold_pct}% / "
        f"recovery>={settings.quota.recovery_threshold_pct}%"
    )
    console.print(f"probe interval : {settings.quota.probe_interval_seconds}s")
    if not settings.ollama.api_key:
        console.print(
            "[yellow]warning: ollama.api_key が未設定です。フェイルオーバー時のリクエストは失敗します。[/yellow]"
        )

    from .server import run_server

    run_server(settings)


def _run_monitor(args: argparse.Namespace) -> None:
    from .monitor import run_monitor

    run_monitor(host=args.host, port=args.port, interval=args.interval)


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.command == "serve":
        _run_serve(args)
    elif args.command == "monitor":
        _run_monitor(args)


if __name__ == "__main__":
    main()
