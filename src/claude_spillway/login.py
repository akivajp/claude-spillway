"""``claude-spillway ollama-login``: sign in to ollama.com and save the session cookie.

Ollama's quota is only shown on the account's settings page, which needs a
signed-in browser session. This command drives a real Chrome through Playwright
with a dedicated, persistent profile: when that profile is still signed in the
cookie is exported headlessly, and otherwise a browser window opens for a person
to sign in (the one step that is never automated - OAuth, 2FA and bot checks all
belong to a human). The cookie is saved together with its expiry, which the
cookie value itself does not reveal.

``claude-spillway ollama-login``: ollama.comにログインし、セッションCookieを保存する。

Ollamaのquotaはアカウントの設定ページにしか表示されず、閲覧にはブラウザの
ログインセッションが必要である。本コマンドはPlaywright経由で実際のChromeを専用の
永続プロファイルで動かす。プロファイルがログイン済みならheadlessのままCookieを
書き出し、そうでなければブラウザを画面に出して人にログインしてもらう(OAuth・2段階
認証・ボット判定はすべて人が扱うべきもので、ここだけは自動化しない)。Cookieは
有効期限と一緒に保存する。期限はCookieの値からは読み取れないため。
"""

from __future__ import annotations

import contextlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from .backends import OLLAMA_SESSION_COOKIE_NAME, OLLAMA_USAGE_PAGE_PATH
from .config import Settings, save_ollama_session
from .i18n import t
from .quota import OllamaSnapshot, parse_ollama_usage_page

console = Console()

#: Browser channels accepted by ``--channel``. "chrome" / "msedge" use the
#: installed browser; "chromium" uses Playwright's own (``playwright install``).
#: ``--channel`` で受け付けるブラウザ。"chrome" / "msedge" はインストール済みの
#: ブラウザを、"chromium" はPlaywright同梱のもの(``playwright install`` が必要)を使う。
BROWSER_CHANNELS = ("chrome", "msedge", "chromium")

#: Seconds between checks for a finished sign-in while the window is open.
#: ブラウザ表示中、ログイン完了を確認する間隔(秒)。
_POLL_SECONDS = 2.0


def user_data_dir() -> Path:
    """Return the per-user data directory, where the browser profile lives.

    ``%LOCALAPPDATA%\\claude-spillway`` on Windows, and
    ``$XDG_DATA_HOME/claude-spillway`` (defaulting to ``~/.local/share``) elsewhere.
    Kept apart from the config directory: a browser profile is bulky state, not
    configuration.

    ブラウザプロファイルを置く、ユーザー単位のデータディレクトリを返す。
    Windowsでは ``%LOCALAPPDATA%\\claude-spillway``、それ以外では
    ``$XDG_DATA_HOME/claude-spillway`` (既定は ``~/.local/share``)。
    ブラウザプロファイルは設定ではなく嵩のある状態なので、設定ディレクトリとは分ける。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "claude-spillway"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "claude-spillway"


def default_profile_dir() -> Path:
    """Return the default browser profile directory for ``ollama-login``.

    ``ollama-login`` が既定で使うブラウザプロファイルのディレクトリを返す。
    """
    return user_data_dir() / "ollama-profile"


def check_session(base_url: str, cookie: str) -> OllamaSnapshot | None:
    """Return the usage reading if ``cookie`` is a live session, else ``None``.

    Same request the proxy makes: a redirect (to sign-in) means the session is
    not valid, and a page without meters is treated the same way.

    ``cookie`` が有効なセッションなら使用状況を、そうでなければ ``None`` を返す。
    プロキシと同じリクエストを送り、(サインインへの)リダイレクトやメーターの無い
    ページは無効とみなす。
    """
    try:
        response = httpx.get(
            base_url.rstrip("/") + OLLAMA_USAGE_PAGE_PATH,
            headers={"cookie": f"{OLLAMA_SESSION_COOKIE_NAME}={cookie}", "accept": "text/html"},
            follow_redirects=False,
            timeout=30.0,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return parse_ollama_usage_page(response.text)


def _session_cookie(context: Any, base_url: str) -> dict[str, Any] | None:
    """Pick the ollama.com session cookie out of a browser context, if present.

    ブラウザコンテキストからollama.comのセッションCookieを取り出す(あれば)。
    """
    for cookie in context.cookies(base_url):
        if cookie.get("name") == OLLAMA_SESSION_COOKIE_NAME and cookie.get("value"):
            return cookie
    return None


def _launch(playwright: Any, profile_dir: Path, channel: str, headless: bool) -> Any:
    """Open the persistent profile in Chrome (or the chosen channel).

    ``--password-store=basic`` keeps cookies readable without the desktop
    keyring, so a profile signed in on the desktop still opens headlessly from
    an SSH session. Dropping ``--enable-automation`` makes the window look like
    an ordinary browser to sign-in pages.

    永続プロファイルをChrome(または指定のブラウザ)で開く。
    ``--password-store=basic`` により、デスクトップのキーリング無しでもCookieを
    読めるようにする。これでデスクトップでログインしたプロファイルを、SSHの
    セッションからheadlessで開ける。``--enable-automation`` を外し、サインイン
    ページから普通のブラウザに見えるようにする。
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_dir.chmod(0o700)
    except OSError:
        # Windows等で権限変更が効かない場合は無視する。
        # Ignore where permissions cannot be changed (e.g. Windows).
        pass
    return playwright.chromium.launch_persistent_context(
        str(profile_dir),
        channel=None if channel == "chromium" else channel,
        headless=headless,
        args=["--password-store=basic"],
        ignore_default_args=["--enable-automation"],
    )


def _export_headless(playwright: Any, profile_dir: Path, channel: str, base_url: str) -> dict[str, Any] | None:
    """Return the profile's session cookie if it is still signed in.

    プロファイルがログイン済みのままなら、そのセッションCookieを返す。
    """
    context = _launch(playwright, profile_dir, channel, headless=True)
    try:
        cookie = _session_cookie(context, base_url)
    finally:
        context.close()
    if cookie is None or check_session(base_url, cookie["value"]) is None:
        return None
    return cookie


def _sign_in(
    playwright: Any, profile_dir: Path, channel: str, base_url: str, timeout: float
) -> dict[str, Any] | None:
    """Open a browser window and wait for a person to sign in.

    Finished means a session cookie the settings page accepts - checked only
    when the cookie value changes, so a stale cookie left in the profile never
    counts. Closing the window gives up.

    ブラウザを画面に出し、人がログインするのを待つ。
    完了の条件は「設定ページが受け付けるセッションCookieが得られたこと」。
    Cookieの値が変わったときだけ確認するため、プロファイルに残った失効済みの
    Cookieを完了と誤認しない。ウィンドウを閉じた場合は中止とみなす。
    """
    context = _launch(playwright, profile_dir, channel, headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(base_url.rstrip("/") + OLLAMA_USAGE_PAGE_PATH)
        deadline = time.monotonic() + timeout
        checked: str | None = None
        while time.monotonic() < deadline:
            try:
                if not context.pages:
                    return None
                cookie = _session_cookie(context, base_url)
            except Exception:  # noqa: BLE001  # 利用者がブラウザごと閉じた / the person closed the browser
                return None
            if cookie is not None and cookie["value"] != checked:
                checked = cookie["value"]
                if check_session(base_url, checked) is not None:
                    return cookie
            # 同期APIはPlaywrightの呼び出し時にイベントを処理するため、待機は sleep でよい。
            # The sync API pumps events on each Playwright call, so a plain sleep is fine.
            time.sleep(_POLL_SECONDS)
        return None
    finally:
        # 利用者が既に閉じていても構わない。/ Fine if the person already closed it.
        with contextlib.suppress(Exception):
            context.close()


def run_ollama_login(
    settings: Settings,
    *,
    profile_dir: Path,
    session_file: Path,
    channel: str,
    headed: bool,
    timeout: float,
) -> int:
    """Sign in (if needed), save the session cookie and its expiry, and report.

    Returns a process exit code.

    必要ならログインし、セッションCookieを有効期限付きで保存して結果を表示する。
    戻り値はプロセスの終了コード。
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        console.print(f"[red]{t('cli.login.no_playwright')}[/red]")
        return 1

    base_url = settings.ollama.base_url
    with sync_playwright() as playwright:
        cookie: dict[str, Any] | None = None
        try:
            if not headed:
                console.print(t("cli.login.checking", profile=profile_dir))
                cookie = _export_headless(playwright, profile_dir, channel, base_url)
            if cookie is None:
                console.print(t("cli.login.opening", timeout=int(timeout)))
                cookie = _sign_in(playwright, profile_dir, channel, base_url, timeout)
        except PlaywrightError as exc:
            # ブラウザ未インストール・画面が無い(SSHのみ)等。原因の行だけ示す。
            # Browser missing, no display (SSH only), ... show the gist only.
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
            console.print(f"[red]{t('cli.login.browser_error', error=first_line)}[/red]")
            console.print(f"[dim]{t('cli.login.browser_hint')}[/dim]")
            return 1

    if cookie is None:
        console.print(f"[red]{t('cli.login.failed')}[/red]")
        return 1

    expires = cookie.get("expires")
    # Playwright は期限なし(セッションCookie)を -1 で表す。
    # Playwright reports a cookie without an expiry as -1.
    expiry = float(expires) if isinstance(expires, (int, float)) and expires > 0 else None
    save_ollama_session(session_file, cookie["value"], expiry)
    console.print(f"[green]{t('cli.login.saved', path=session_file)}[/green]")
    if expiry is not None:
        date = datetime.fromtimestamp(expiry, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")
        console.print(t("cli.login.expires", date=date, days=int((expiry - time.time()) // 86400)))
    snapshot = check_session(base_url, cookie["value"])
    if snapshot is not None:
        console.print(
            t(
                "cli.login.quota",
                session=_pct(snapshot.session_utilization),
                weekly=_pct(snapshot.weekly_utilization),
            )
        )
    if settings.ollama.session_cookie.strip():
        # 明示指定のCookieがあると保存ファイルは使われないため、その旨を警告する。
        # An explicit cookie in the config overrides the saved file; say so.
        console.print(f"[yellow]{t('cli.login.overridden')}[/yellow]")
    else:
        console.print(f"[dim]{t('cli.login.picked_up')}[/dim]")
    return 0


def _pct(utilization: float | None) -> str:
    return "-" if utilization is None else f"{utilization * 100:.1f}%"
