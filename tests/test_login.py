"""Unit tests for the parts of login.py that need no browser.

ブラウザを必要としない login.py の部分の単体テスト。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from claude_spillway import login
from claude_spillway.login import check_session, default_profile_dir, user_data_dir

_PAGE = '<div data-usage-track="" aria-label="Weekly usage 20% used"></div>'


def test_profile_dir_lives_in_the_user_data_dir(tmp_path: Path) -> None:
    # conftest が XDG_DATA_HOME / LOCALAPPDATA を tmp_path/data に向けている
    # conftest points XDG_DATA_HOME / LOCALAPPDATA at tmp_path/data
    assert user_data_dir() == tmp_path / "data" / "claude-spillway"
    assert default_profile_dir() == user_data_dir() / "ollama-profile"


@pytest.mark.parametrize(
    ("response", "valid"),
    [
        (httpx.Response(200, text=_PAGE), True),
        # サインインへの転送 = 無効 / a bounce to sign-in means invalid
        (httpx.Response(303, headers={"location": "https://ollama.com/signin"}), False),
        # メーターの無いページも無効 / a page without meters is invalid too
        (httpx.Response(200, text="<html>sign in</html>"), False),
    ],
)
def test_check_session(monkeypatch: pytest.MonkeyPatch, response: httpx.Response, valid: bool) -> None:
    seen: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        seen["url"] = url
        seen.update(kwargs)
        return response

    monkeypatch.setattr(login.httpx, "get", fake_get)
    result = check_session("https://ollama.com/", "abc")
    assert (result is not None) is valid
    assert seen["url"] == "https://ollama.com/settings"
    assert seen["headers"]["cookie"] == "__Secure-session=abc"  # type: ignore[index]
    # 失効の検知のため、リダイレクトは追わない / redirects are not followed, to see expiry
    assert seen["follow_redirects"] is False
