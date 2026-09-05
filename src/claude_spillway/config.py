"""Settings models backing the YAML config file and CLI overrides.

YAML設定ファイルとCLI上書きを扱う設定モデル群。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Environment variable naming an explicit config file, checked before the
#: OS-standard location. Handy for systemd units and scheduled tasks.
#: OS標準の場所より先に参照される、設定ファイルを明示指定するための環境変数。
#: systemdのunitやタスクスケジューラから指定するのに便利。
CONFIG_ENV_VAR = "CLAUDE_SPILLWAY_CONFIG"

#: File name looked up inside the user config directory.
#: ユーザー設定ディレクトリ内で探されるファイル名。
CONFIG_FILE_NAME = "config.yaml"


def user_config_dir() -> Path:
    """Return the OS-standard per-user config directory for this tool.

    ``%APPDATA%\\claude-spillway`` on Windows, and
    ``$XDG_CONFIG_HOME/claude-spillway`` (defaulting to ``~/.config``) elsewhere.

    このツール用の、OS標準のユーザー単位設定ディレクトリを返す。
    Windowsでは ``%APPDATA%\\claude-spillway``、それ以外では
    ``$XDG_CONFIG_HOME/claude-spillway`` (既定は ``~/.config``)。
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        # APPDATA is effectively always set on Windows, but fall back to the
        # canonical location rather than raising if it is missing.
        # WindowsではAPPDATAはまず設定されているが、無い場合も例外にせず既定の場所へ倒す。
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "claude-spillway"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "claude-spillway"


def default_config_candidates() -> list[Path]:
    """List the config paths searched when ``--config`` is not given, in order.

    ``--config`` が指定されなかった場合に探索する設定ファイルパスを、優先順に返す。
    """
    candidates: list[Path] = []
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        candidates.append(Path(from_env).expanduser())
    candidates.append(user_config_dir() / CONFIG_FILE_NAME)
    return candidates


def resolve_config_path(explicit: Path | None) -> Path | None:
    """Decide which config file to load.

    An explicit ``--config`` wins and is returned even when it does not exist,
    so the caller can warn about a typo instead of silently using defaults.
    ``None`` means no config file was found and built-in defaults apply.

    どの設定ファイルを読み込むかを決定する。
    ``--config`` の明示指定は最優先で、存在しない場合もそのまま返す
    (呼び出し側が「黙ってデフォルトを使う」のではなく警告を出せるようにするため)。
    ``None`` は設定ファイルが見つからず、組み込みのデフォルト値を使うことを意味する。
    """
    if explicit is not None:
        return explicit
    for candidate in default_config_candidates():
        if candidate.exists():
            return candidate
    return None



def _expand_env_vars(value: Any) -> Any:
    """Replace ``${ENV_VAR}`` in config values with the process environment.

    This lets the config file reference an API key instead of embedding it.

    設定値中の ``${ENV_VAR}`` をプロセス環境変数の値で置換する
    (APIキーを設定ファイルに直書きせずに済むようにするため)。
    """
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")

        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


class ListenConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787


class AnthropicConfig(BaseModel):
    base_url: str = "https://api.anthropic.com"


class OllamaConfig(BaseModel):
    base_url: str = "https://ollama.com"
    api_key: str = ""


class QuotaConfig(BaseModel):
    #: Fail over to Ollama once the remaining ratio drops below this percentage.
    #: この残量%%を下回ったらOllamaへフェイルオーバーする
    fallback_threshold_pct: float = 10.0
    #: Switch back to Anthropic at this percentage (hysteresis: prevents flapping).
    #: この残量%%まで回復したらAnthropicへ切り戻す(ヒステリシス。flapping防止)
    recovery_threshold_pct: float = 20.0
    #: Interval between recovery probes while in fallback mode, in seconds.
    #: フォールバック中、Anthropicの回復確認を行う間隔(秒)
    probe_interval_seconds: float = 60.0
    #: max_tokens for the recovery probe (keep it tiny to barely consume quota).
    #: 回復確認プローブに使うmax_tokens(できるだけ小さくしてquota消費を抑える)
    probe_max_tokens: int = 1
    #: Model used by the recovery probe (a lightweight one is recommended).
    #: 回復確認プローブに使うモデル(軽量モデル推奨)
    probe_model: str = "claude-haiku-4-5-20251001"


class ModelMappingRule(BaseModel):
    #: fnmatch-style glob pattern (e.g. "claude-opus-*").
    #: fnmatch形式のグロブパターン(例: "claude-opus-*")
    match: str
    #: Ollama-side model name to use when the pattern matches.
    #: マッチした場合に使うOllama側のモデル名
    target: str


class ModelMappingConfig(BaseModel):
    #: Fallback used when no rule matches.
    #: どのルールにもマッチしなかった場合のデフォルト
    default: str = "gpt-oss:120b"
    rules: list[ModelMappingRule] = Field(default_factory=list)

    def resolve(self, requested_model: str) -> str:
        """Map an Anthropic model name to the Ollama-side model name.

        Anthropicのモデル名からOllama側のモデル名を決定する。
        """
        for rule in self.rules:
            if fnmatch.fnmatch(requested_model, rule.match):
                return rule.target
        return self.default


class Settings(BaseModel):
    listen: ListenConfig = Field(default_factory=ListenConfig)
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    model_mapping: ModelMappingConfig = Field(default_factory=ModelMappingConfig)
    log_level: str = "INFO"

    @classmethod
    def load(cls, config_path: Path | None) -> Settings:
        """Build :class:`Settings` from a YAML file, or from defaults alone.

        A missing or unspecified path is not an error: every field has a default.

        YAMLファイルを読み込んで :class:`Settings` を構築する。
        ファイル未指定/未存在時はエラーとせず、デフォルト値のみで構築する。
        """
        data: dict[str, Any] = {}
        if config_path is not None and config_path.exists():
            with config_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if loaded:
                    data = loaded
        data = _expand_env_vars(data)
        return cls.model_validate(data)
