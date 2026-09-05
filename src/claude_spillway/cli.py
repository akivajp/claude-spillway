"""``claude-spillway`` command line entry point. / ``claude-spillway`` コマンドラインエントリーポイント。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from .config import Settings, resolve_config_path
from .i18n import t

console = Console()


def _build_arg_parser() -> argparse.ArgumentParser:
    # All help text goes through the message catalog so that the CLI speaks
    # English by default and Japanese only under a Japanese locale.
    # ヘルプ文言はすべてメッセージカタログ経由にして、既定は英語、
    # 日本語ロケールのときだけ日本語になるようにしている。
    parser = argparse.ArgumentParser(
        prog="claude-spillway",
        description=t("cli.description"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help=t("cli.serve.help"))
    serve.add_argument("-c", "--config", type=Path, default=None, help=t("cli.serve.config"))
    serve.add_argument("--host", type=str, default=None, help=t("cli.serve.host"))
    serve.add_argument("--port", type=int, default=None, help=t("cli.serve.port"))
    serve.add_argument(
        "--fallback-threshold-pct",
        type=float,
        default=None,
        help=t("cli.serve.fallback_threshold"),
    )
    serve.add_argument(
        "--recovery-threshold-pct",
        type=float,
        default=None,
        help=t("cli.serve.recovery_threshold"),
    )
    serve.add_argument("--log-level", type=str, default=None, help=t("cli.serve.log_level"))

    monitor = subparsers.add_parser("monitor", help=t("cli.monitor.help"))
    monitor.add_argument("--host", type=str, default="127.0.0.1", help=t("cli.monitor.host"))
    monitor.add_argument("--port", type=int, default=8787, help=t("cli.monitor.port"))
    monitor.add_argument("--interval", type=float, default=1.0, help=t("cli.monitor.interval"))

    dashboard = subparsers.add_parser("dashboard", help=t("cli.dashboard.help"))
    dashboard.add_argument("--host", type=str, default="127.0.0.1", help=t("cli.dashboard.host"))
    dashboard.add_argument("--port", type=int, default=8787, help=t("cli.dashboard.port"))
    dashboard.add_argument("--no-open", action="store_true", help=t("cli.dashboard.no_open"))

    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    # CLI flags win over the YAML config file.
    # コマンドラインの指定は設定ファイルより優先される。
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
    # Without --config, fall back to $CLAUDE_SPILLWAY_CONFIG and then the
    # per-user config directory, so a service unit can just run "serve".
    # --config 未指定時は $CLAUDE_SPILLWAY_CONFIG → ユーザー設定ディレクトリ の順に
    # 探索する。これによりサービス定義側は "serve" だけで済む。
    config_path = resolve_config_path(args.config)
    if config_path is not None and not config_path.exists():
        # Only reachable for an explicit --config: warn instead of silently
        # falling back to the defaults, which would hide a typo.
        # ここに来るのは --config 明示指定時のみ。黙ってデフォルトに倒すと
        # パスの打ち間違いに気づけないため警告する。
        console.print(f"[yellow]{t('cli.warn.config_missing', path=config_path)}[/yellow]")

    # Banner text: make "specified but missing" visibly different from "loaded".
    # バナー表示: 「指定されたが存在しない」と「読み込んだ」を見た目で区別する。
    if config_path is None:
        config_display = t("cli.config.none")
    elif config_path.exists():
        config_display = str(config_path)
    else:
        config_display = f"{config_path} {t('cli.config.none')}"

    settings = Settings.load(config_path)
    settings = _apply_overrides(settings, args)
    _setup_logging(settings.log_level)

    # The startup banner only prints field names and values, so it stays
    # identical in every locale.
    # 起動時バナーは項目名と値だけなので、どのロケールでも表示は共通。
    console.rule("[bold cyan]claude-spillway[/bold cyan]")
    console.print(f"config         : {config_display}")
    console.print(f"listen         : {settings.listen.host}:{settings.listen.port}")
    console.print(
        f"dashboard      : http://{settings.listen.host}:{settings.listen.port}/_spillway/"
    )
    console.print(f"anthropic      : {settings.anthropic.base_url}")
    console.print(f"ollama         : {settings.ollama.base_url}")
    console.print(
        f"thresholds     : fallback<{settings.quota.fallback_threshold_pct}% / "
        f"recovery>={settings.quota.recovery_threshold_pct}%"
    )
    console.print(f"probe interval : {settings.quota.probe_interval_seconds}s")
    if not settings.ollama.api_key:
        console.print(f"[yellow]{t('cli.warn.no_ollama_key')}[/yellow]")

    from .server import run_server

    run_server(settings)


def _run_monitor(args: argparse.Namespace) -> None:
    from .monitor import run_monitor

    run_monitor(host=args.host, port=args.port, interval=args.interval)


def _run_dashboard(args: argparse.Namespace) -> None:
    """Print the dashboard URL and, unless asked not to, open it in a browser.

    The URL is always printed first: on WSL and on headless boxes there may be
    no browser to open, and the user still needs the address.

    ダッシュボードのURLを表示し、指定が無ければブラウザで開く。
    URLを先に必ず表示するのは、WSLやGUIの無い環境では開けないことがあり、
    その場合でもアドレス自体は必要になるため。
    """
    import webbrowser

    url = f"http://{args.host}:{args.port}/_spillway/"
    console.print(url)
    if args.no_open:
        return
    try:
        opened = webbrowser.open(url)
    except (webbrowser.Error, OSError):
        # ブラウザが1つも無い環境では webbrowser.Error、開く側のプロセス起動に
        # 失敗すると OSError。URLは既に出しているのでここで落とす理由はない。
        # webbrowser.Error when no browser exists at all, OSError when spawning
        # one fails. The URL is already printed, so nothing here is worth
        # failing over.
        opened = False
    if not opened:
        console.print(f"[yellow]{t('cli.dashboard.open_failed')}[/yellow]")


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.command == "serve":
        _run_serve(args)
    elif args.command == "monitor":
        _run_monitor(args)
    elif args.command == "dashboard":
        _run_dashboard(args)


if __name__ == "__main__":
    main()
