"""``claude-spillway monitor``: TUI that shows the state of a running proxy.

``claude-spillway monitor``: 稼働中プロキシの状態を表示するTUI。
"""

from __future__ import annotations

import json
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


def _fmt_reset(ts: float | None) -> str:
    """Render a reset time as a local clock time plus how long until then.

    The absolute time answers "when can I use it again"; the countdown answers
    "how long do I have to wait", and both are wanted at a glance.

    リセット時刻を「ローカル時刻 + あとどれくらいか」の形で表示する。
    絶対時刻は「いつ使えるようになるか」、カウントダウンは「あとどれだけ待つか」に
    答えるもので、一目で両方を知りたいため併記する。
    """
    if ts is None:
        return "-"
    clock = datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%H:%M")
    remaining = int(ts - datetime.now(tz=UTC).timestamp())
    if remaining <= 0:
        # 既に過ぎている(次のリクエストで新しい値が入る)。
        # Already past; the next reading will carry a fresh value.
        return clock
    hours, minutes = divmod(remaining // 60, 60)
    delta = f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"
    return f"{clock} ({t('monitor.reset.in', delta=delta)})"


def _build_renderable(data: dict, url: str, error: str | None) -> Group:
    mode = data.get("mode", "unknown")
    mode_label = {
        "anthropic": "[bold green]ANTHROPIC[/bold green]",
        "fallback": "[bold yellow]OLLAMA (fallback)[/bold yellow]",
    }.get(mode, mode)

    # 観測値の取得元。使用量エンドポイント経由ならquotaを消費していないことが分かる。
    # Where the reading came from: the usage endpoint means no quota was spent on it.
    source = (data.get("anthropic") or {}).get("source")
    source_line = f"\nsource   : {t('monitor.source.' + source)}" if source else ""

    # updated は現在時刻ではなく最新の観測時刻を表示する。現在時刻にすると
    # 毎フレーム再描画の誘因になり、ちらつきを生むため。
    # Show the freshest observation, not the wall clock: a running clock would
    # force a redraw every frame and flicker with it.
    observed = max(
        (v for v in (anthropic.get("observed_at"), ollama.get("observed_at")) if v), default=None
    ) if (anthropic := data.get("anthropic", {}), ollama := data.get("ollama", {})) else None
    updated = _fmt_time(observed) if observed else datetime.now(tz=UTC).astimezone().strftime("%H:%M:%S")
    header = Panel(
        f"backend  : {mode_label}\n"
        f"endpoint : {url}\n"
        f"updated  : {updated}"
        f"{source_line}",
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
    table.add_column(t("monitor.col.resets_at"), justify="right")
    table.add_column(t("monitor.col.observed_at"), justify="right")
    table.add_row(
        t("monitor.row.window_5h"),
        _fmt_pct(_remaining(anthropic.get("utilization_5h"))),
        _fmt_reset(anthropic.get("reset_5h")),
        _fmt_time(anthropic.get("observed_at")),
    )
    table.add_row(
        t("monitor.row.window_7d"),
        _fmt_pct(_remaining(anthropic.get("utilization_7d"))),
        _fmt_reset(anthropic.get("reset_7d")),
        _fmt_time(anthropic.get("observed_at")),
    )
    if anthropic.get("requests_remaining_ratio") is not None:
        table.add_row(
            t("monitor.row.requests"),
            _fmt_pct(anthropic.get("requests_remaining_ratio")),
            "",
            _fmt_time(anthropic.get("observed_at")),
        )
    if anthropic.get("tokens_remaining_ratio") is not None:
        table.add_row(
            t("monitor.row.tokens"),
            _fmt_pct(anthropic.get("tokens_remaining_ratio")),
            "",
            _fmt_time(anthropic.get("observed_at")),
        )
    table.add_row(t("monitor.row.min_remaining"), _fmt_pct(anthropic.get("remaining_ratio")), "", "")

    ollama_table = Table(title=t("monitor.ollama.title"), show_lines=False)
    ollama_table.add_column(t("monitor.col.item"))
    ollama_table.add_column(t("monitor.col.value"), justify="right")
    ollama_table.add_column(t("monitor.col.observed_at"), justify="right")
    # アカウント全体のquota。Ollamaはリセット時刻を返さないため列は設けない。
    # Account-wide quota. Ollama reports no reset times, so there is no column for them.
    ollama_table.add_row(
        t("monitor.row.session_window"),
        _fmt_pct(_remaining(ollama.get("session_utilization"))),
        _fmt_time(ollama.get("observed_at")),
    )
    ollama_table.add_row(
        t("monitor.row.weekly_window"),
        _fmt_pct(_remaining(ollama.get("weekly_utilization"))),
        _fmt_time(ollama.get("observed_at")),
    )
    top_models = ollama.get("weekly_models") or []
    if top_models:
        summary = ", ".join(f"{m['name']} ({m['request_count']})" for m in top_models[:3])
        ollama_table.add_row(t("monitor.row.top_models"), summary, "")
    # ここから下はこのプロキシが中継した分のみの自己計測値。
    # Below this point: self-measured counters covering only what this proxy relayed.
    ollama_table.add_row(t("monitor.row.relayed"), str(ollama.get("requests_sent", 0)), "")
    ollama_table.add_row(t("monitor.row.failed"), str(ollama.get("requests_failed", 0)), "")
    failures = ollama.get("consecutive_failures") or 0
    if failures:
        ollama_table.add_row(t("monitor.row.consecutive_failures"), f"[red]{failures}[/red]", "")
    ollama_table.add_row(t("monitor.row.last_status"), str(ollama.get("last_status_code") or "-"), "")
    ollama_table.add_row(
        t("monitor.row.last_request_at"), _fmt_time(ollama.get("last_request_at")), ""
    )
    if ollama.get("last_error"):
        ollama_table.add_row(t("monitor.row.last_error"), f"[red]{ollama['last_error']}[/red]", "")

    footer = Panel(
        t(
            "monitor.footer.thresholds",
            fallback=thresholds.get("fallback_pct", "?"),
            recovery=thresholds.get("recovery_pct", "?"),
        )
        + "\n"
        + t(
            "monitor.footer.policy",
            policy=data.get("policy", "?"),
            reason=data.get("reason", "?"),
        ),
        border_style="dim",
    )

    return Group(header, table, ollama_table, footer)


def run_monitor(host: str, port: int, interval: float) -> None:
    """Poll a running claude-spillway on the given host/port and render its state.

    Redrawing is the only thing a TUI can do badly: doing it faster than the
    data changes makes the panel flicker without telling the user anything, so
    the automatic refresh is disabled and a frame is rendered only when the
    status payload actually differs from the previous one.

    指定ホスト・ポートで稼働中のclaude-spillwayの状態を定期ポーリングして表示する。
    TUIで失败しやすいのは再描画の方である。データが変化していないのに高頻度で
    再描画すると、何の情報も増やさずに画面がちらつく。そのため自動再描画を無効化し、
    statusの内容が前回と異なるときだけフレームを描き直す。
    """
    url = f"http://{host}:{port}/_spillway/status"
    console = Console()
    client = httpx.Client(timeout=5.0)
    # 直近フレームの指紋。statusのJSONとエラー文言が同一なら再描画しない。
    # Fingerprint of the last rendered frame: same payload and error, no redraw.
    last_frame: tuple[str, str | None] | None = None

    try:
        # auto_refresh=False: 自前の4Hz再描画がちらつきの原因だった。
        # auto_refresh=False: its own 4 Hz redraw was the flicker.
        with Live(console=console, auto_refresh=False, screen=False) as live:
            while True:
                error: str | None = None
                data: dict = {}
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPError as exc:
                    error = t("monitor.error.body", url=url, error=exc)
                # uptime_seconds は取得のたびに増えるため指紋から除外する。
                # これが含まれると毎回「変化あり」扱いになり、比較が無意味になる。
                # uptime_seconds grows on every poll, so it is excluded from the
                # fingerprint: kept in, every poll would count as a change.
                if isinstance(data.get("uptime_seconds"), (int, float)):
                    stable = {k: v for k, v in data.items() if k != "uptime_seconds"}
                else:
                    stable = data
                frame = (json.dumps(stable, sort_keys=True), error)
                if frame != last_frame:
                    last_frame = frame
                    live.update(_build_renderable(data, url, error), refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
