"""``claude-spillway monitor``: 稼働中プロキシの状態を表示するTUI。"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


def _remaining(utilization: float | None) -> float | None:
    if utilization is None:
        return None
    return 1.0 - utilization


def _fmt_pct(ratio: float | None) -> str:
    if ratio is None:
        return "-"
    pct = ratio * 100
    color = "green" if pct >= 30 else ("yellow" if pct >= 10 else "red")
    return f"[{color}]{pct:5.1f}%[/{color}]"


def _fmt_time(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%H:%M:%S")


def _build_renderable(data: dict, url: str, error: str | None) -> Group:
    mode = data.get("mode", "unknown")
    mode_label = {
        "anthropic": "[bold green]ANTHROPIC[/bold green]",
        "fallback": "[bold yellow]OLLAMA (fallback)[/bold yellow]",
    }.get(mode, mode)

    header = Panel(
        f"backend  : {mode_label}\n"
        f"endpoint : {url}\n"
        f"updated  : {datetime.now(tz=UTC).astimezone().strftime('%H:%M:%S')}",
        title="claude-spillway monitor",
        border_style="cyan",
    )

    if error is not None:
        return Group(header, Panel(f"[red]{error}[/red]", title="接続エラー", border_style="red"))

    anthropic = data.get("anthropic", {})
    ollama = data.get("ollama", {})

    if anthropic.get("observed_at") is None and ollama.get("last_request_at") is None:
        # このプロキシはAnthropicの認証情報を保持せず、Claude Codeからの実リクエストを
        # 中継して初めてレスポンスヘッダーからquotaを観測できる設計のため、
        # 一度もリクエストが通っていない間は数値が出せない。その旨を明示する。
        return Group(
            header,
            Panel(
                "まだこのプロキシを経由したリクエストが観測されていません。\n"
                "ANTHROPIC_BASE_URL をこのプロキシに向けて Claude Code から\n"
                "何かリクエストを送ると、ここにquota状況が表示されます。",
                title="待機中",
                border_style="yellow",
            ),
        )
    thresholds = data.get("thresholds", {})

    table = Table(title="Anthropic quota", show_lines=False)
    table.add_column("指標")
    table.add_column("残量", justify="right")
    table.add_column("観測時刻", justify="right")
    table.add_row(
        "5時間窓",
        _fmt_pct(_remaining(anthropic.get("utilization_5h"))),
        _fmt_time(anthropic.get("observed_at")),
    )
    table.add_row(
        "週次窓",
        _fmt_pct(_remaining(anthropic.get("utilization_7d"))),
        _fmt_time(anthropic.get("observed_at")),
    )
    if anthropic.get("requests_remaining_ratio") is not None:
        table.add_row(
            "リクエスト数(APIキー課金時)",
            _fmt_pct(anthropic.get("requests_remaining_ratio")),
            _fmt_time(anthropic.get("observed_at")),
        )
    if anthropic.get("tokens_remaining_ratio") is not None:
        table.add_row(
            "トークン数(APIキー課金時)",
            _fmt_pct(anthropic.get("tokens_remaining_ratio")),
            _fmt_time(anthropic.get("observed_at")),
        )
    table.add_row("判定に使う最小残量", _fmt_pct(anthropic.get("remaining_ratio")), "")

    ollama_table = Table(title="Ollama Cloud (自己計測値。公式quota APIは存在しません)", show_lines=False)
    ollama_table.add_column("項目")
    ollama_table.add_column("値", justify="right")
    ollama_table.add_row("中継リクエスト数", str(ollama.get("requests_sent", 0)))
    ollama_table.add_row("失敗数", str(ollama.get("requests_failed", 0)))
    ollama_table.add_row("直近ステータスコード", str(ollama.get("last_status_code") or "-"))
    ollama_table.add_row("直近リクエスト時刻", _fmt_time(ollama.get("last_request_at")))
    if ollama.get("last_error"):
        ollama_table.add_row("直近エラー", f"[red]{ollama['last_error']}[/red]")

    footer = Panel(
        f"fallback閾値: 残り{thresholds.get('fallback_pct', '?')}% 未満で切替 / "
        f"recovery閾値: 残り{thresholds.get('recovery_pct', '?')}%以上で復帰",
        border_style="dim",
    )

    return Group(header, table, ollama_table, footer)


def run_monitor(host: str, port: int, interval: float) -> None:
    """指定ホスト・ポートで稼働中のclaude-spillwayの状態を定期ポーリングして表示する。"""
    url = f"http://{host}:{port}/_spillway/status"
    console = Console()
    client = httpx.Client(timeout=5.0)

    try:
        with Live(console=console, refresh_per_second=4, screen=False) as live:
            while True:
                error: str | None = None
                data: dict = {}
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPError as exc:
                    error = f"{url} に接続できません: {exc}"
                live.update(_build_renderable(data, url, error))
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
