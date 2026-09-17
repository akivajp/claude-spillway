"""Ollama Cloud model names: discovery, direct-route matching, picker sync.

Two jobs that share one piece of knowledge - which model names Ollama Cloud
serves - and would otherwise duplicate the spelling rules between them:

1. deciding that a requested model belongs to Ollama and must not be relayed
   to Anthropic (:class:`DirectModelRegistry`), and
2. writing those names into Claude Code's ``/model`` picker
   (:func:`build_picker_options`, :func:`sync_model_picker`).

Ollama Cloudのモデル名に関する処理: 一覧取得、直結判定、ピッカー同期。
「Ollama Cloudがどのモデル名を提供しているか」という同じ知識を使う2つの役割を
ここにまとめている(分けると表記ゆれの規則が二重実装になるため):

1. 要求されたモデルがOllama側のものであり、Anthropicへ中継してはならないと
   判断すること(:class:`DirectModelRegistry`)
2. それらの名前をClaude Codeの ``/model`` ピッカーへ書き出すこと
   (:func:`build_picker_options`, :func:`sync_model_picker`)
"""

from __future__ import annotations

import fnmatch
import json
import time
from pathlib import Path
from typing import Any

from .config import DirectModelsConfig, ModelPickerConfig

#: Suffixes Ollama uses to mark a cloud-hosted model. Both spellings resolve to
#: the same model, so they are stripped before comparing names.
#: Ollamaがクラウド実行のモデルを示すために使うサフィックス。どちらの表記も
#: 同じモデルを指すため、名前を比較する前に取り除く。
_CLOUD_SUFFIXES = (":cloud", "-cloud")

#: Claude Code's marker for the 1M-context variant of a model it knows. It is
#: meaningless to Ollama, so it is dropped rather than forwarded.
#: Claude Codeが既知モデルの1Mコンテキスト版を指すために付ける印。Ollama側には
#: 意味を持たないため、転送せずに取り除く。
_EXTENDED_CONTEXT_SUFFIX = "[1m]"

#: Names that must never be treated as an Ollama model, whatever a listing or
#: the config says. A typo here would send Claude traffic to Ollama silently.
#: 一覧や設定が何と言おうと、絶対にOllamaのモデルとして扱ってはならない名前。
#: ここを取り違えると、Claude宛のトラフィックが黙ってOllamaへ流れてしまう。
_RESERVED_NAMES = frozenset(
    {
        "default",
        "inherit",
        "best",
        "opus",
        "opusplan",
        "sonnet",
        "haiku",
        "fable",
        "mythos",
    }
)

#: Prefixes belonging to Anthropic, including the Bedrock/Vertex spellings.
#: Anthropic側の接頭辞。Bedrock/Vertex形式の表記も含む。
_RESERVED_PREFIXES = ("claude", "anthropic.", "us.anthropic.", "eu.anthropic.")

#: Key this tool owns inside Claude Code's settings file. Everything else in
#: that file is left exactly as it was found.
#: Claude Codeの設定ファイル内で、このツールが書き換えるキー。
#: それ以外のキーは読み込んだ状態のまま保持する。
SETTINGS_PICKER_KEY = "modelPicker"


def strip_extended_context_suffix(name: str) -> str:
    """Drop Claude Code's ``[1m]`` marker from ``name`` if present.

    ``name`` に付いたClaude Codeの ``[1m]`` 印を取り除く。
    """
    text = name.strip()
    if text.lower().endswith(_EXTENDED_CONTEXT_SUFFIX):
        return text[: -len(_EXTENDED_CONTEXT_SUFFIX)].strip()
    return text


def normalize_model_name(name: str) -> str:
    """Reduce a model name to the form used for comparison.

    ``GLM-5.3-Flash:cloud``, ``glm-5.3-flash`` and ``glm-5.3-flash[1m]`` all
    name the same model, so they must all compare equal.

    比較に用いる形へモデル名を正規化する。
    ``GLM-5.3-Flash:cloud`` / ``glm-5.3-flash`` / ``glm-5.3-flash[1m]`` は
    いずれも同じモデルを指すため、比較上は一致しなければならない。
    """
    text = strip_extended_context_suffix(name).lower()
    for suffix in _CLOUD_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def cloud_model_name(base: str) -> str:
    """Return ``base`` spelled the way Ollama refers to a cloud-hosted model.

    Which suffix applies depends on whether the name already carries a tag:
    ``glm-5.3-flash`` becomes ``glm-5.3-flash:cloud``, while ``gemma4:31b``
    becomes ``gemma4:31b-cloud`` because its tag slot is taken.

    Ollamaがクラウド実行のモデルを指す際の表記で ``base`` を返す。
    どちらのサフィックスになるかは、その名前が既にタグを持つかで決まる。
    ``glm-5.3-flash`` は ``glm-5.3-flash:cloud`` に、``gemma4:31b`` はタグの
    位置が埋まっているため ``gemma4:31b-cloud`` になる。
    """
    text = base.strip()
    if not text:
        return text
    if normalize_model_name(text) != text.lower():
        # Already carries a cloud suffix; leave it alone.
        # 既にクラウド用サフィックスが付いているのでそのまま返す。
        return text
    return f"{text}-cloud" if ":" in text else f"{text}:cloud"


def is_reserved_name(name: str) -> bool:
    """Return whether ``name`` is an Anthropic model or one of Claude Code's aliases.

    ``name`` がAnthropicのモデル、またはClaude Codeのエイリアスかどうかを返す。
    """
    normalized = normalize_model_name(name)
    if normalized in _RESERVED_NAMES:
        return True
    return normalized.startswith(_RESERVED_PREFIXES)


def parse_ollama_models(payload: Any) -> list[str]:
    """Extract the model names from Ollama's tag listing.

    The endpoint is undocumented like the usage one, so a shape that is not
    understood yields an empty list rather than raising: the proxy then simply
    routes the way it did before discovery existed.

    Ollamaのタグ一覧からモデル名を取り出す。
    使用量エンドポイントと同様に非公開APIであるため、想定外の形が返ってきた
    場合は例外にせず空リストを返す(直結判定が働かなくなるだけで、従来どおりの
    ルーティングで動作は続く)。
    """
    if not isinstance(payload, dict):
        return []
    entries = payload.get("models")
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("name") or entry.get("model")
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or is_reserved_name(name):
            continue
        key = normalize_model_name(name)
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return sorted(names)


class DirectModelRegistry:
    """Knows which model names must bypass Anthropic and go straight to Ollama.

    どのモデル名がAnthropicを経由せず直接Ollamaへ向かうべきかを保持する。
    """

    def __init__(self, config: DirectModelsConfig) -> None:
        self._config = config
        #: Names as Ollama reported them, for display and for the picker.
        #: Ollamaが返したままの名前。表示およびピッカー生成に使う。
        self._discovered: list[str] = []
        self._discovered_at: float | None = None
        #: Normalized lookup set covering both the discovered names and the
        #: manually configured extras.
        #: 取得した名前と手動指定の追加分の両方を含む、正規化済みの照合用集合。
        self._lookup: set[str] = set()
        self._rebuild_lookup()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def discovery_enabled(self) -> bool:
        return self._config.enabled and self._config.discover

    @property
    def refresh_interval_seconds(self) -> float:
        return self._config.refresh_interval_seconds

    @property
    def discovered(self) -> list[str]:
        """Model names most recently reported by Ollama Cloud.

        直近にOllama Cloudから取得したモデル名。
        """
        return list(self._discovered)

    @property
    def discovered_at(self) -> float | None:
        return self._discovered_at

    def _rebuild_lookup(self) -> None:
        lookup = {normalize_model_name(name) for name in self._discovered}
        lookup.update(normalize_model_name(name) for name in self._config.extra)
        # A reserved name in the config must not take effect: routing Claude
        # traffic into Ollama would be far worse than ignoring a config line.
        # 設定に予約名が書かれていても効かせない。Claude宛のトラフィックを
        # Ollamaへ流してしまう方が、設定を1行無視するよりはるかに害が大きい。
        self._lookup = {name for name in lookup if name and not is_reserved_name(name)}

    def update(self, names: list[str]) -> None:
        """Replace the discovered model list with a fresh reading.

        An empty reading is ignored: a transient hiccup should not silently
        disable direct routing and start relaying those names to Anthropic,
        where they can only 404.

        取得済みのモデル一覧を最新の読み取り結果で置き換える。
        空の結果は無視する。一時的な失敗で直結判定が黙って無効化され、404に
        しかならないAnthropicへ流れ始めるのを避けるため。
        """
        if not names:
            return
        self._discovered = list(names)
        self._discovered_at = time.time()
        self._rebuild_lookup()

    def is_direct(self, model: str | None) -> bool:
        """Return whether ``model`` should be sent straight to Ollama Cloud.

        ``model`` をOllama Cloudへ直接送るべきかどうかを返す。
        """
        if not self._config.enabled or not model:
            return False
        normalized = normalize_model_name(model)
        if not normalized or is_reserved_name(normalized):
            return False
        return normalized in self._lookup

    def outbound_name(self, model: str) -> str:
        """Return the name to put in the request sent to Ollama.

        The requested spelling is kept as-is - Ollama Cloud accepts both the
        bare and the suffixed form - except for Claude Code's ``[1m]`` marker,
        which Ollama would fail to resolve.

        Ollamaへ送るリクエストに入れる名前を返す。
        要求された表記をそのまま使う(素の名前・サフィックス付きのどちらも
        Ollama Cloudは受け付けるため)。ただしClaude Codeの ``[1m]`` 印だけは、
        Ollama側でモデルを解決できなくなるため取り除く。
        """
        return strip_extended_context_suffix(model)


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def select_picker_models(models: list[str], config: ModelPickerConfig) -> list[str]:
    """Apply the include/exclude filters to the discovered model names.

    取得したモデル名に include/exclude のフィルタを適用する。
    """
    selected = []
    for name in models:
        if config.include and not _matches_any(name, config.include):
            continue
        if config.exclude and _matches_any(name, config.exclude):
            continue
        selected.append(name)
    return selected


def build_picker_options(models: list[str], config: ModelPickerConfig) -> list[dict[str, str]]:
    """Build the ``modelPicker.options`` rows for the given model names.

    ``behavesAs`` is always written. Claude Code will not offer a row for a
    model this release has no catalog entry for unless it is told which known
    model's client-side handling to borrow, so omitting it produces a picker
    that silently lacks every row this function just built.

    渡されたモデル名から ``modelPicker.options`` の各行を組み立てる。
    ``behavesAs`` は常に書き出す。Claude Codeは、そのリリースがカタログを
    持たないモデルについて「どの既知モデルのクライアント側の扱いを流用するか」を
    指定されない限り行を提供しないため、省略するとここで作った行がすべて
    黙って表示されないピッカーができあがる。
    """
    options: list[dict[str, str]] = []
    for base in select_picker_models(models, config):
        model = cloud_model_name(base) if config.use_cloud_suffix else base
        if _matches_any(base, config.extended_context):
            # Claude Codeに1Mトークンとして扱わせるための印。これが無いと
            # behaves_as のモデルの窓長(200k)で圧縮されてしまう。
            # 送出直前に DirectModelRegistry.outbound_name が取り除く。
            # The marker that makes Claude Code manage a 1M-token context;
            # without it the window of the behaves_as model (200k) is assumed.
            # DirectModelRegistry.outbound_name strips it before it is sent.
            model = f"{model}{_EXTENDED_CONTEXT_SUFFIX}"
        override = next(
            (rule for rule in config.overrides if fnmatch.fnmatch(base, rule.match)),
            None,
        )
        label = (override.label if override and override.label else config.label_template).format(
            model=model, base=base
        )
        description = (
            override.description
            if override and override.description
            else config.description_template
        ).format(model=model, base=base)
        behaves_as = (
            override.behaves_as if override and override.behaves_as else config.behaves_as
        )
        options.append(
            {
                "model": model,
                "label": label,
                "description": description,
                "behavesAs": behaves_as,
            }
        )
    return options


def read_settings(path: Path) -> dict[str, Any]:
    """Load Claude Code's settings file, or ``{}`` when it does not exist yet.

    Claude Codeの設定ファイルを読み込む。未作成なら ``{}`` を返す。
    """
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise TypeError(f"{path} does not contain a JSON object")
    return data


def apply_picker(
    settings: dict[str, Any], options: list[dict[str, str]], config: ModelPickerConfig
) -> dict[str, Any]:
    """Return ``settings`` with only the picker key replaced.

    A shallow copy is enough because nothing below the replaced key is shared.

    ``settings`` のうちピッカーのキーだけを差し替えたものを返す。
    差し替えるキーの下に共有されるオブジェクトが無いため、浅いコピーで足りる。
    """
    updated = dict(settings)
    updated[SETTINGS_PICKER_KEY] = {
        "options": options,
        "replaceBuiltInOptions": config.replace_built_in_options,
    }
    return updated


def sync_model_picker(
    models: list[str],
    config: ModelPickerConfig,
    *,
    dry_run: bool = False,
) -> tuple[Path, list[dict[str, str]], Path | None]:
    """Write the picker rows into Claude Code's settings file.

    Returns the file written, the rows, and the backup that was taken (``None``
    when nothing was written, or when the file did not exist yet). The file is
    replaced atomically so an interrupted run cannot leave Claude Code with a
    settings file it refuses to parse.

    ピッカーの行をClaude Codeの設定ファイルへ書き出す。
    戻り値は「書き込んだファイル」「行の内容」「取得したバックアップ」
    (書き込みを行わなかった場合、およびファイルが元々存在しない場合は ``None``)。
    書き込みは原子的に行う。中断によってClaude Codeが読めない設定ファイルが
    残ることを防ぐため。
    """
    path = Path(config.settings_path).expanduser()
    options = build_picker_options(models, config)
    settings = read_settings(path)
    updated = apply_picker(settings, options, config)
    if dry_run:
        return path, options, None

    backup: Path | None = None
    if path.exists():
        backup = path.with_name(f"{path.name}.spillway-bak-{time.strftime('%Y%m%d%H%M%S')}")
        backup.write_bytes(path.read_bytes())
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(f"{path.name}.spillway-tmp")
    temporary.write_text(
        json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path, options, backup
