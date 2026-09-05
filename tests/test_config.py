"""Unit tests for config file discovery.

設定ファイルの探索ロジックの単体テスト。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_spillway.config import (
    CONFIG_ENV_VAR,
    Settings,
    default_config_candidates,
    resolve_config_path,
    user_config_dir,
)


@pytest.fixture(autouse=True)
def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own environment out of the discovery tests.

    開発者自身の環境変数が探索テストに影響しないようにする。
    """
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)


def test_user_config_dir_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert user_config_dir() == tmp_path / "claude-spillway"


def test_user_config_dir_defaults_to_dot_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert user_config_dir() == tmp_path / ".config" / "claude-spillway"


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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    candidates = default_config_candidates()
    assert candidates[0] == from_env
    assert candidates[-1] == tmp_path / "xdg" / "claude-spillway" / "config.yaml"
    assert resolve_config_path(None) == from_env


def test_falls_back_to_user_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "claude-spillway" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("log_level: WARNING\n", encoding="utf-8")

    assert resolve_config_path(None) == config
    assert Settings.load(resolve_config_path(None)).log_level == "WARNING"


def test_returns_none_when_nothing_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    assert resolve_config_path(None) is None
    # None は「デフォルト値のみで動く」を意味し、エラーではない。
    # None means "run on defaults alone" and is not an error.
    assert Settings.load(None).listen.port == 8787
