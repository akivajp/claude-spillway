"""``claude-spillway monitor``: TUI that shows the state of a running proxy.

``claude-spillway monitor``: 稼働中プロキシの状態を表示するTUI。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .i18n import t


def _remaining(utilization: float | None) -> float | None:
    # The status endpoint reports utilization (used ratio); the TUI shows the
    # remaining ratio, so invert it here.
    # ステータスAPIは使用率を返すが、TUIでは残量を表示するためここで反転する。
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
    # Timestamps come over the wire as UTC epoch seconds; show them in the
    # viewer's local time zone.
    # タイムスタンプはUTCのエポック秒で受け取るため、表示は実行環境のローカル時刻に直す。
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
        return Group(
            header,
            Panel(f"[red]{error}[/red]", title=t("monitor.error.title"), border_style="red"),
        )

    anthropic = data.get("anthropic", {})
    ollama = data.get("ollama", {})

    if anthropic.get("observed_at") is None and ollama.get("last_request_at") is None:
        # This proxy holds no Anthropic credentials of its own: it can only
        # read quota from the response headers of real requests Claude Code
        # sends through it. Say so explicitly while nothing has passed yet.
        # このプロキシはAnthropicの認証情報を保持せず、Claude Codeからの実リクエストを
        # 中継して初めてレスポンスヘッダーからquotaを観測できる設計のため、
        # 一度もリクエストが通っていない間は数値が出せない。その旨を明示する。
        return Group(
            header,
            Panel(
                t("monitor.waiting.body"),
                title=t("monitor.waiting.title"),
                border_style="yellow",
            ),
        )
    thresholds = data.get("thresholds", {})

    table = Table(title=t("monitor.quota.title"), show_lines=False)
    table.add_column(t("monitor.col.metric"))
    table.add_column(t("monitor.col.remaining"), justify="right")
    table.add_column(t("monitor.col.observed_at"), justify="right")
    table.add_row(
        t("monitor.row.window_5h"),
        _fmt_pct(_remaining(anthropic.get("utilization_5h"))),
        _fmt_time(anthropic.get("observed_at")),
    )
    table.add_row(
        t("monitor.row.window_7d"),
        _fmt_pct(_remaining(anthropic.get("utilization_7d"))),
        _fmt_time(anthropic.get("observed_at")),
    )
    if anthropic.get("requests_remaining_ratio") is not None:
        table.add_row(
            t("monitor.row.requests"),
            _fmt_pct(anthropic.get("requests_remaining_ratio")),
            _fmt_time(anthropic.get("observed_at")),
        )
    if anthropic.get("tokens_remaining_ratio") is not None:
        table.add_row(
            t("monitor.row.tokens"),
            _fmt_pct(anthropic.get("tokens_remaining_ratio")),
            _fmt_time(anthropic.get("observed_at")),
        )
    table.add_row(t("monitor.row.min_remaining"), _fmt_pct(anthropic.get("remaining_ratio")), "")

    ollama_table = Table(title=t("monitor.ollama.title"), show_lines=False)
    ollama_table.add_column(t("monitor.col.item"))
    ollama_table.add_column(t("monitor.col.value"), justify="right")
    ollama_table.add_row(t("monitor.row.relayed"), str(ollama.get("requests_sent", 0)))
    ollama_table.add_row(t("monitor.row.failed"), str(ollama.get("requests_failed", 0)))
    ollama_table.add_row(t("monitor.row.last_status"), str(ollama.get("last_status_code") or "-"))
    ollama_table.add_row(t("monitor.row.last_request_at"), _fmt_time(ollama.get("last_request_at")))
    if ollama.get("last_error"):
        ollama_table.add_row(t("monitor.row.last_error"), f"[red]{ollama['last_error']}[/red]")

    footer = Panel(
        t(
            "monitor.footer.thresholds",
            fallback=thresholds.get("fallback_pct", "?"),
            recovery=thresholds.get("recovery_pct", "?"),
        ),
        border_style="dim",
    )

    return Group(header, table, ollama_table, footer)


def run_monitor(host: str, port: int, interval: float) -> None:
    """Poll a running claude-spillway on the given host/port and render its state.

    指定ホスト・ポートで稼働中のclaude-spillwayの状態を定期ポーリングして表示する。
    """
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
                    error = t("monitor.error.body", url=url, error=exc)
                live.update(_build_renderable(data, url, error))
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
