"""Unit tests for config file discovery.

設定ファイルの探索ロジックの単体テスト。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_spillway.config import (
    CONFIG_ENV_VAR,
    OllamaConfig,
    Settings,
    default_config_candidates,
    load_ollama_session,
    resolve_config_path,
    save_ollama_session,
    user_config_dir,
)


@pytest.fixture(autouse=True)
def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own environment out of the discovery tests.

    開発者自身の環境変数が探索テストに影響しないようにする。
    """
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)


def _point_user_config_dir_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Aim :func:`user_config_dir` at ``root`` on whichever OS we run on.

    The lookup reads ``%APPDATA%`` on Windows and ``$XDG_CONFIG_HOME`` elsewhere,
    so set both and let the platform pick.

    実行OSに関わらず :func:`user_config_dir` が ``root`` を指すようにする。
    探索はWindowsでは ``%APPDATA%``、それ以外では ``$XDG_CONFIG_HOME`` を見るため、
    両方を設定してプラットフォーム側に選ばせる。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setenv("APPDATA", str(root))
    return root / "claude-spillway"


@pytest.mark.skipif(os.name == "nt", reason="XDG paths are POSIX-only / XDGはPOSIX専用")
def test_user_config_dir_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert user_config_dir() == tmp_path / "claude-spillway"


@pytest.mark.skipif(os.name == "nt", reason="XDG paths are POSIX-only / XDGはPOSIX専用")
def test_user_config_dir_defaults_to_dot_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert user_config_dir() == tmp_path / ".config" / "claude-spillway"


@pytest.mark.skipif(os.name != "nt", reason="APPDATA is Windows-only / APPDATAはWindows専用")
def test_user_config_dir_uses_appdata_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert user_config_dir() == tmp_path / "claude-spillway"


def test_explicit_path_always_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit --config is returned even when it does not exist, so the
    CLI can warn about a typo instead of silently using the defaults.

    --config の明示指定は存在しなくてもそのまま返る(打ち間違いを黙って
    デフォルトに倒さず、CLI側で警告できるようにするため)。
    """
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "from-env.yaml"))
    explicit = tmp_path / "does-not-exist.yaml"
    assert resolve_config_path(explicit) == explicit


def test_env_var_is_searched_before_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from_env = tmp_path / "from-env.yaml"
    from_env.write_text("log_level: DEBUG\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(from_env))
    user_dir = _point_user_config_dir_at(monkeypatch, tmp_path / "user")

    candidates = default_config_candidates()
    assert candidates[0] == from_env
    assert candidates[-1] == user_dir / "config.yaml"
    assert resolve_config_path(None) == from_env


def test_falls_back_to_user_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _point_user_config_dir_at(monkeypatch, tmp_path) / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("log_level: WARNING\n", encoding="utf-8")

    assert resolve_config_path(None) == config
    assert Settings.load(resolve_config_path(None)).log_level == "WARNING"


def test_returns_none_when_nothing_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _point_user_config_dir_at(monkeypatch, tmp_path / "empty")
    assert resolve_config_path(None) is None
    # None は「デフォルト値のみで動く」を意味し、エラーではない。
    # None means "run on defaults alone" and is not an error.
    assert Settings.load(None).listen.port == 8787


def test_saved_ollama_session_round_trips_owner_only(tmp_path: Path) -> None:
    """保存したセッションは期限ごと読み戻せ、所有者のみ読める権限で作られること。

    A saved session reads back with its expiry, in a file only the owner can read.
    """
    path = tmp_path / "nested" / "ollama-session.json"
    save_ollama_session(path, "abc", 1800000000.0)
    session = load_ollama_session(OllamaConfig(session_file=str(path)))
    assert session is not None
    assert (session.cookie, session.expires, session.source) == ("abc", 1800000000.0, "file")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_explicit_session_cookie_wins_over_the_saved_file(tmp_path: Path) -> None:
    path = tmp_path / "ollama-session.json"
    save_ollama_session(path, "from-file", 1800000000.0)
    session = load_ollama_session(OllamaConfig(session_cookie="from-config", session_file=str(path)))
    assert session is not None
    # 明示指定は期限を持たない / an explicit cookie carries no expiry
    assert (session.cookie, session.expires, session.source) == ("from-config", None, "config")


def test_missing_or_broken_session_file_means_no_session(tmp_path: Path) -> None:
    path = tmp_path / "ollama-session.json"
    assert load_ollama_session(OllamaConfig(session_file=str(path))) is None
    path.write_text("{not json")
    assert load_ollama_session(OllamaConfig(session_file=str(path))) is None
    path.write_text('{"cookie": "", "expires": 1}')
    assert load_ollama_session(OllamaConfig(session_file=str(path))) is None


def test_session_file_defaults_to_the_user_config_dir() -> None:
    assert OllamaConfig().session_file_path() == user_config_dir() / "ollama-session.json"
