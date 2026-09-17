"""``claude-spillway`` command line entry point. / ``claude-spillway`` コマンドラインエントリーポイント。"""

from __future__ import annotations

import argparse
import fnmatch
import json
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

    models = subparsers.add_parser("models", help=t("cli.models.help"))
    models.add_argument("-c", "--config", type=Path, default=None, help=t("cli.models.config"))
    models.add_argument(
        "--sync-picker", action="store_true", help=t("cli.models.sync_picker")
    )
    models.add_argument("--settings", type=Path, default=None, help=t("cli.models.settings"))
    models.add_argument("--dry-run", action="store_true", help=t("cli.models.dry_run"))

    monitor = subparsers.add_parser("monitor", help=t("cli.monitor.help"))
    monitor.add_argument("--host", type=str, default="127.0.0.1", help=t("cli.monitor.host"))
    monitor.add_argument("--port", type=int, default=8787, help=t("cli.monitor.port"))
    monitor.add_argument("--interval", type=float, default=1.0, help=t("cli.monitor.interval"))

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


def _load_settings(config_path: Path | None) -> Settings:
    """Resolve and load the config file, warning about an explicit path that is missing.

    設定ファイルを解決して読み込む。明示指定されたパスが存在しない場合は警告する。
    """
    resolved = resolve_config_path(config_path)
    if resolved is not None and not resolved.exists():
        console.print(f"[yellow]{t('cli.warn.config_missing', path=resolved)}[/yellow]")
    return Settings.load(resolved)


def _fetch_ollama_models(settings: Settings) -> list[str] | None:
    """Read the Ollama Cloud model listing, printing why and returning ``None`` on failure.

    Ollama Cloudのモデル一覧を取得する。失敗時は理由を表示して ``None`` を返す。
    """
    import httpx

    from .backends import OLLAMA_TAGS_PATH
    from .models import parse_ollama_models

    if not settings.ollama.api_key:
        console.print(f"[red]{t('cli.models.error.no_key')}[/red]")
        return None
    url = f"{settings.ollama.base_url.rstrip('/')}{OLLAMA_TAGS_PATH}"
    try:
        response = httpx.get(
            url,
            headers={
                "authorization": f"Bearer {settings.ollama.api_key}",
                "accept": "application/json",
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        console.print(f"[red]{t('cli.models.error.request', error=exc)}[/red]")
        return None
    if response.status_code != 200:
        console.print(f"[red]{t('cli.models.error.status', status=response.status_code)}[/red]")
        return None
    names = parse_ollama_models(response.json())
    if not names:
        console.print(f"[red]{t('cli.models.error.empty')}[/red]")
        return None
    return names


def _run_models(args: argparse.Namespace) -> None:
    """Show the Ollama Cloud models, and optionally register them with Claude Code.

    Ollama Cloudのモデルを一覧表示し、必要に応じてClaude Codeへ登録する。
    """
    from rich.table import Table

    from .models import (
        SETTINGS_PICKER_KEY,
        cloud_model_name,
        select_picker_models,
        sync_model_picker,
    )

    settings = _load_settings(args.config)
    picker = settings.direct_models.picker
    if args.settings is not None:
        # コマンドラインの指定は設定ファイルより優先される(serveと同じ方針)。
        # The command line wins over the config file, as it does for serve.
        picker = picker.model_copy(update={"settings_path": str(args.settings)})

    names = _fetch_ollama_models(settings)
    if names is None:
        raise SystemExit(1)

    selected = set(select_picker_models(names, picker))
    table = Table(title=t("cli.models.table.title"))
    table.add_column(t("cli.models.col.model"), style="cyan", no_wrap=True)
    table.add_column(t("cli.models.col.discovered"), style="dim", no_wrap=True)
    table.add_column(t("cli.models.col.picker"), justify="center")
    table.add_column(t("cli.models.col.behaves_as"), style="dim", no_wrap=True)
    for base in names:
        model = cloud_model_name(base) if picker.use_cloud_suffix else base
        override = next(
            (rule for rule in picker.overrides if fnmatch.fnmatch(base, rule.match)), None
        )
        behaves_as = override.behaves_as if override and override.behaves_as else picker.behaves_as
        included = base in selected
        table.add_row(
            model,
            base,
            f"[green]{t('cli.models.picker.included')}[/green]"
            if included
            else f"[dim]{t('cli.models.picker.excluded')}[/dim]",
            behaves_as if included else "",
        )
    console.print(table)

    direct = settings.direct_models.enabled
    console.print(
        f"[green]{t('cli.models.direct.on')}[/green]"
        if direct
        else f"[yellow]{t('cli.models.direct.off')}[/yellow]"
    )

    if not args.sync_picker:
        console.print(f"[dim]{t('cli.models.picker.hint')}[/dim]")
        return

    # 空のまま書き込むと /model からOllamaの行が消えるだけなので、警告して止める。
    # Writing an empty list would merely strip every Ollama row from /model, so
    # warn and stop rather than doing it silently.
    if not selected:
        console.print(f"[yellow]{t('cli.models.picker.empty')}[/yellow]")
        raise SystemExit(1)

    # 既存のsettings.jsonが壊れている/読めない場合に、生のトレースバックではなく
    # どのファイルで何が起きたかを出す。書き込み先がユーザーの設定ファイルであり、
    # 原因の分からない失敗はそのまま手作業での調査になるため。
    # Report which file failed and why rather than a raw traceback: the target
    # is the user's own settings file, and an unexplained failure there turns
    # straight into manual investigation.
    try:
        path, options, backup = sync_model_picker(names, picker, dry_run=args.dry_run)
    except (OSError, ValueError, TypeError) as exc:
        target = Path(picker.settings_path).expanduser()
        console.print(f"[red]{t('cli.models.error.settings', path=target, error=exc)}[/red]")
        raise SystemExit(1) from exc
    if args.dry_run:
        console.print(f"[yellow]{t('cli.models.picker.dry_run', path=path)}[/yellow]")
        console.print_json(json.dumps({SETTINGS_PICKER_KEY: {"options": options}}))
        return
    console.print(
        t("cli.models.picker.written", count=len(options), setting=SETTINGS_PICKER_KEY, path=path)
    )
    if backup is not None:
        console.print(f"[dim]{t('cli.models.picker.backup', path=backup)}[/dim]")
    console.print(t("cli.models.picker.next"))


def _run_monitor(args: argparse.Namespace) -> None:
    from .monitor import run_monitor

    run_monitor(host=args.host, port=args.port, interval=args.interval)


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.command == "serve":
        _run_serve(args)
    elif args.command == "models":
        _run_models(args)
    elif args.command == "monitor":
        _run_monitor(args)


if __name__ == "__main__":
    main()
