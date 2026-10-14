"""Shared pytest fixtures. / テスト共通のfixture。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the per-user config/data directories at a temporary directory.

    The proxy reads a saved ollama.com session from the user config directory,
    so without this a session saved on the developer's machine would leak into
    the tests (and real cookies would be sent to the mock transports).

    ユーザー単位の設定・データディレクトリを一時ディレクトリに向ける。
    プロキシはユーザー設定ディレクトリから保存済みのollama.comセッションを読むため、
    これが無いと開発機に保存したセッションがテストに混入する(実際のCookieが
    モックのトランスポートへ送られる)。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_SPILLWAY_CONFIG", raising=False)
