"""Tests for the browser dashboard served at ``/_spillway/``.

The page itself is static, so what is worth testing is the wiring around it:
that the catch-all proxy route does not swallow it, that the labels really get
substituted, and that the HTML ships as package data.

``/_spillway/`` で配信するブラウザ用ダッシュボードのテスト。
ページ自体は静的なので、検証する価値があるのはその周辺の配線である。
すなわち、総当たりのプロキシ経路に飲み込まれないこと、文言が実際に差し込まれる
こと、HTMLがパッケージデータとして同梱されることの3点。
"""

from __future__ import annotations

import httpx
import pytest

from claude_spillway.backends import ProxyBackends
from claude_spillway.config import Settings
from claude_spillway.i18n import get_language, negotiate_language, set_language
from claude_spillway.proxy_app import _dashboard_cache, create_app, render_dashboard


@pytest.fixture
def settings() -> Settings:
    s = Settings.load(None)
    s.ollama.api_key = "test-ollama-key"
    return s


@pytest.fixture(autouse=True)
def _reset_language():
    # 言語はプロセス全体でキャッシュされるため、テスト間で必ず戻す。
    # The language is cached process-wide, so always restore it between tests.
    yield
    _dashboard_cache.clear()
    set_language(None)


def _unreachable(request: httpx.Request) -> httpx.Response:
    # ダッシュボードの取得でバックエンドへ中継が起きたら、それ自体が不具合。
    # Any relay to a backend while fetching the dashboard is itself the bug.
    raise AssertionError(f"the dashboard must not be relayed upstream: {request.url}")


@pytest.mark.parametrize("path", ["/_spillway", "/_spillway/"])
async def test_dashboard_is_served_on_both_spellings(settings: Settings, path: str) -> None:
    """Both spellings must be served locally rather than proxied to Anthropic.

    FastAPI's trailing-slash redirect only fires when no route matched, and the
    catch-all route matches everything, so the slash-less form is easy to lose.

    末尾スラッシュの有無どちらもローカルで配信されること。FastAPIの自動リダイレクトは
    「どのルートにも一致しない場合」にしか働かず、総当たりルートは何にでも一致するため、
    スラッシュ無しの形は取りこぼしやすい。
    """
    transport = httpx.MockTransport(_unreachable)
    backends = ProxyBackends(settings, anthropic_transport=transport, ollama_transport=transport)
    app = create_app(settings, backends=backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(path)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<title>claude-spillway</title>" in resp.text
    await backends.aclose()


async def test_dashboard_reads_the_status_endpoint_it_ships_with(settings: Settings) -> None:
    """The page must point at the status path this app actually registers.

    ページが参照するパスが、このアプリが実際に登録しているものと一致すること。
    """
    transport = httpx.MockTransport(_unreachable)
    backends = ProxyBackends(settings, anthropic_transport=transport, ollama_transport=transport)
    app = create_app(settings, backends=backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        page = (await client.get("/_spillway/")).text
        status = await client.get("/_spillway/status")

    assert '"/_spillway/status"' in page
    assert status.status_code == 200
    await backends.aclose()


def test_no_placeholder_survives_rendering() -> None:
    """A missed substitution would ship a page whose JavaScript cannot parse.

    置換漏れがあるとJavaScriptが構文エラーになるため、残っていないことを確認する。
    """
    set_language("en")
    _dashboard_cache.clear()
    page = render_dashboard()
    assert "__CS_LANG__" not in page
    assert "__CS_LABELS__" not in page


@pytest.mark.parametrize(
    ("language", "expected"),
    [("en", "OLLAMA (fallback)"), ("ja", "OLLAMA (フォールバック中)")],
)
def test_labels_follow_the_configured_language(language: str, expected: str) -> None:
    """Labels come from the shared catalog, so the page follows the locale.

    文言は共通カタログ由来であり、ページもロケールに追従すること。
    """
    set_language(language)
    _dashboard_cache.clear()
    page = render_dashboard()
    assert expected in page
    assert f'<html lang="{language}"' in page


def test_dashboard_html_ships_as_package_data() -> None:
    """Guard against a packaging change that would drop the .html from the wheel.

    wheelから .html が抜け落ちるようなパッケージング変更に対する保険。
    """
    from importlib import resources

    resource = resources.files("claude_spillway").joinpath("dashboard.html")
    assert resource.is_file()
    assert "<!doctype html>" in resource.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("ja", "ja"),
        ("ja-JP,ja;q=0.9,en-US;q=0.8", "ja"),
        ("en-GB,en;q=0.9", "en"),
        # 品質値の順が記述順と食い違う場合は品質値が勝つ。
        # Quality wins over the written order when the two disagree.
        ("en;q=0.5,ja;q=0.9", "ja"),
        # q=0 は「この言語は要らない」の意なので候補から外す。
        # q=0 means "not this one", so it must not be selected.
        ("ja;q=0, en", "en"),
        ("*", None),
        ("de,fr;q=0.8", None),
        ("", None),
        (None, None),
    ],
)
def test_accept_language_negotiation(header: str | None, expected: str | None) -> None:
    """The header drives the page's language, including its quality values.

    ヘッダーの品質値まで含めてページの言語が決まること。
    """
    assert negotiate_language(header) == expected


async def test_browser_language_wins_over_the_process_locale(settings: Settings) -> None:
    """A browser asking for Japanese gets Japanese from an English process.

    This is the case that matters in practice: the proxy runs as a systemd user
    service with LANG=C.UTF-8, so without this the page is always English.

    英語ロケールのプロセスでも、日本語を要求したブラウザには日本語を返すこと。
    実運用で効くのはこの経路である。プロキシは LANG=C.UTF-8 のsystemdユーザー
    サービスとして動くため、これが無いとページは常に英語になる。
    """
    set_language("en")
    transport = httpx.MockTransport(_unreachable)
    backends = ProxyBackends(settings, anthropic_transport=transport, ollama_transport=transport)
    app = create_app(settings, backends=backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        japanese = await client.get("/_spillway/", headers={"accept-language": "ja-JP,ja;q=0.9"})
        english = await client.get("/_spillway/", headers={"accept-language": "en-US"})
        unspecified = await client.get("/_spillway/")

    assert '<html lang="ja"' in japanese.text
    assert "OLLAMA (フォールバック中)" in japanese.text
    assert '<html lang="en"' in english.text
    # ヘッダーが無ければプロセスのロケールに戻る。
    # With no header, it falls back to the process locale.
    assert '<html lang="en"' in unspecified.text
    # ページの言語交渉がプロセス全体の言語を書き換えてしまわないこと。
    # Negotiating a page's language must not rewrite the process-wide one.
    assert get_language() == "en"
    assert japanese.headers["vary"] == "Accept-Language"
    await backends.aclose()
