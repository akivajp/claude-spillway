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
from claude_spillway.i18n import set_language
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
