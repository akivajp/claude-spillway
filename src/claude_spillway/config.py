"""YAML設定ファイルとCLI上書きを扱う設定モデル群。"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(value: Any) -> Any:
    """設定値中の ``${ENV_VAR}`` をプロセス環境変数の値で置換する(APIキーの直書き回避用)。"""
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
    #: この残量%%を下回ったらOllamaへフェイルオーバーする
    fallback_threshold_pct: float = 10.0
    #: この残量%%まで回復したらAnthropicへ切り戻す(ヒステリシス。flapping防止)
    recovery_threshold_pct: float = 20.0
    #: フォールバック中、Anthropicの回復確認を行う間隔(秒)
    probe_interval_seconds: float = 60.0
    #: 回復確認プローブに使うmax_tokens(できるだけ小さくしてquota消費を抑える)
    probe_max_tokens: int = 1
    #: 回復確認プローブに使うモデル(軽量モデル推奨)
    probe_model: str = "claude-haiku-4-5-20251001"


class ModelMappingRule(BaseModel):
    #: fnmatch形式のグロブパターン(例: "claude-opus-*")
    match: str
    #: マッチした場合に使うOllama側のモデル名
    target: str


class ModelMappingConfig(BaseModel):
    #: どのルールにもマッチしなかった場合のデフォルト
    default: str = "gpt-oss:120b"
    rules: list[ModelMappingRule] = Field(default_factory=list)

    def resolve(self, requested_model: str) -> str:
        """Anthropicのモデル名からOllama側のモデル名を決定する。"""
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
        """YAMLファイルを読み込んで :class:`Settings` を構築する。ファイル未指定/未存在時はデフォルト値のみ。"""
        data: dict[str, Any] = {}
        if config_path is not None and config_path.exists():
            with config_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if loaded:
                    data = loaded
        data = _expand_env_vars(data)
        return cls.model_validate(data)
