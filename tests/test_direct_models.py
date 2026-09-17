"""Direct (name-based) routing to Ollama Cloud and the /model picker sync.

モデル名によるOllama Cloudへの直結ルーティングと、/model ピッカーの同期のテスト。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_spillway.backends import ProxyBackends
from claude_spillway.cli import _split_patterns, toggle_picks
from claude_spillway.config import Settings
from claude_spillway.models import (
    SETTINGS_PICKER_KEY,
    DirectModelRegistry,
    build_picker_options,
    cloud_model_name,
    is_reserved_name,
    normalize_model_name,
    parse_ollama_models,
    registered_picker_models,
    sync_model_picker,
)
from claude_spillway.proxy_app import create_app

TAGS_PAYLOAD = {
    "models": [
        {"name": "glm-5.3-flash", "model": "glm-5.3-flash"},
        {"name": "gemma4:31b", "model": "gemma4:31b"},
        {"name": "gpt-oss:120b", "model": "gpt-oss:120b"},
    ]
}


def _settings(**direct: object) -> Settings:
    return Settings.model_validate(
        {
            "ollama": {"api_key": "test-key"},
            "direct_models": direct,
        }
    )


# --- name handling --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("glm-5.3-flash", "glm-5.3-flash"),
        ("glm-5.3-flash:cloud", "glm-5.3-flash"),
        ("GLM-5.3-Flash:cloud", "glm-5.3-flash"),
        ("gemma4:31b-cloud", "gemma4:31b"),
        # Claude Code appends this to a model it believes has a 1M variant;
        # Ollama cannot resolve it, so it must not survive normalization.
        # Claude Codeが1M版を持つと認識したモデルに付ける印。Ollama側では
        # 解決できないため、正規化後に残ってはならない。
        ("glm-5.3-flash[1m]", "glm-5.3-flash"),
        ("  glm-5.3-flash  ", "glm-5.3-flash"),
    ],
)
def test_normalize_model_name(raw: str, expected: str) -> None:
    assert normalize_model_name(raw) == expected


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        # No tag yet, so ":cloud" fills the tag slot.
        # タグがまだ無いため ":cloud" がタグの位置に入る。
        ("glm-5.3-flash", "glm-5.3-flash:cloud"),
        # The tag slot is taken, so the suffix is appended to the tag instead.
        # タグの位置が埋まっているため、サフィックスはタグ側に付く。
        ("gemma4:31b", "gemma4:31b-cloud"),
        ("glm-5.3-flash:cloud", "glm-5.3-flash:cloud"),
        ("gemma4:31b-cloud", "gemma4:31b-cloud"),
    ],
)
def test_cloud_model_name(base: str, expected: str) -> None:
    assert cloud_model_name(base) == expected


@pytest.mark.parametrize(
    "name",
    ["claude-opus-5", "claude-sonnet-5[1m]", "opus", "sonnet", "default", "us.anthropic.claude-x"],
)
def test_reserved_names_are_never_ollama(name: str) -> None:
    assert is_reserved_name(name)


def test_parse_ollama_models_ignores_unexpected_shapes() -> None:
    assert parse_ollama_models(TAGS_PAYLOAD) == ["gemma4:31b", "glm-5.3-flash", "gpt-oss:120b"]
    assert parse_ollama_models({"models": "nope"}) == []
    assert parse_ollama_models({}) == []
    assert parse_ollama_models([]) == []


# --- registry -------------------------------------------------------------


def test_registry_matches_discovered_names_in_any_spelling() -> None:
    registry = DirectModelRegistry(_settings().direct_models)
    registry.update(parse_ollama_models(TAGS_PAYLOAD))

    assert registry.is_direct("glm-5.3-flash")
    assert registry.is_direct("glm-5.3-flash:cloud")
    assert registry.is_direct("gemma4:31b-cloud")
    assert not registry.is_direct("claude-opus-5")
    assert not registry.is_direct("some-model-nobody-serves")
    assert not registry.is_direct(None)


def test_registry_honours_extra_and_refuses_reserved_names() -> None:
    registry = DirectModelRegistry(
        _settings(extra=["private-model:cloud", "claude-opus-5"]).direct_models
    )

    assert registry.is_direct("private-model")
    # Listing a Claude model as "extra" must not divert Claude traffic.
    # Claudeのモデルを extra に書いても、Claude宛のトラフィックを逸らしてはならない。
    assert not registry.is_direct("claude-opus-5")


def test_registry_keeps_previous_list_on_an_empty_reading() -> None:
    registry = DirectModelRegistry(_settings().direct_models)
    registry.update(["glm-5.3-flash"])
    registry.update([])
    assert registry.is_direct("glm-5.3-flash")


def test_registry_disabled_matches_nothing() -> None:
    registry = DirectModelRegistry(_settings(enabled=False).direct_models)
    registry.update(["glm-5.3-flash"])
    assert not registry.is_direct("glm-5.3-flash")


def test_outbound_name_drops_the_extended_context_marker() -> None:
    registry = DirectModelRegistry(_settings().direct_models)
    assert registry.outbound_name("glm-5.3-flash:cloud[1m]") == "glm-5.3-flash:cloud"


# --- routing --------------------------------------------------------------


class _Recorder:
    """Captures where each request went and what model it carried.

    各リクエストの行き先と、載っていたモデル名を記録する。
    """

    def __init__(self) -> None:
        self.anthropic: list[str | None] = []
        self.ollama: list[str | None] = []

    @staticmethod
    def _model(request: httpx.Request) -> str | None:
        if not request.content:
            return None
        return json.loads(request.content).get("model")

    def anthropic_handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/oauth/usage"):
            return httpx.Response(404)
        self.anthropic.append(self._model(request))
        return httpx.Response(
            200,
            json={"ok": True},
            headers={
                "anthropic-ratelimit-unified-5h-utilization": "0.1",
                "anthropic-ratelimit-unified-7d-utilization": "0.1",
            },
        )

    def ollama_handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS_PAYLOAD)
        if request.url.path == "/api/usage":
            return httpx.Response(404)
        self.ollama.append(self._model(request))
        return httpx.Response(200, json={"ok": True})


def _client(settings: Settings, recorder: _Recorder) -> TestClient:
    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(recorder.anthropic_handler),
        ollama_transport=httpx.MockTransport(recorder.ollama_handler),
    )
    return TestClient(create_app(settings, backends))


def test_ollama_model_goes_straight_to_ollama_with_full_anthropic_quota() -> None:
    recorder = _Recorder()
    settings = _settings()
    with _client(settings, recorder) as client:
        response = client.post(
            "/v1/messages",
            json={"model": "glm-5.3-flash:cloud", "messages": [], "max_tokens": 1},
        )

    assert response.status_code == 200
    # Anthropic quota is at 90% remaining, so quota-based routing would have
    # sent this to Anthropic; the model name is what diverted it.
    # Anthropicのquotaは90%残っており、quota基準なら当然Anthropicへ行く。
    # 行き先を変えたのはモデル名である。
    assert recorder.anthropic == []
    assert recorder.ollama == ["glm-5.3-flash:cloud"]


def test_claude_model_still_goes_to_anthropic() -> None:
    recorder = _Recorder()
    with _client(_settings(), recorder) as client:
        client.post(
            "/v1/messages",
            json={"model": "claude-opus-5", "messages": [], "max_tokens": 1},
        )

    assert recorder.anthropic == ["claude-opus-5"]
    assert recorder.ollama == []


def test_direct_routing_disabled_relays_the_name_to_anthropic() -> None:
    recorder = _Recorder()
    with _client(_settings(enabled=False), recorder) as client:
        client.post(
            "/v1/messages",
            json={"model": "glm-5.3-flash:cloud", "messages": [], "max_tokens": 1},
        )

    assert recorder.anthropic == ["glm-5.3-flash:cloud"]
    assert recorder.ollama == []


def test_extended_context_marker_is_stripped_before_reaching_ollama() -> None:
    recorder = _Recorder()
    with _client(_settings(), recorder) as client:
        client.post(
            "/v1/messages",
            json={"model": "glm-5.3-flash:cloud[1m]", "messages": [], "max_tokens": 1},
        )

    assert recorder.ollama == ["glm-5.3-flash:cloud"]


def test_count_tokens_for_a_direct_model_is_relayed_under_the_stand_in() -> None:
    recorder = _Recorder()
    with _client(_settings(), recorder) as client:
        client.post(
            "/v1/messages/count_tokens",
            json={"model": "glm-5.3-flash:cloud", "messages": []},
        )

    # Anthropic would 404 on the Ollama name, leaving the context meter blind,
    # and Ollama's shim is unstable for this endpoint - hence the stand-in.
    # AnthropicはOllamaの名前に404を返し、コンテキスト残量の表示が盲目になる。
    # またOllamaの互換シムはこのエンドポイントで不安定なため、代役を立てる。
    assert recorder.anthropic == ["claude-sonnet-5"]
    assert recorder.ollama == []


def test_count_tokens_stand_in_can_be_turned_off() -> None:
    recorder = _Recorder()
    with _client(_settings(count_tokens_stand_in=""), recorder) as client:
        client.post(
            "/v1/messages/count_tokens",
            json={"model": "glm-5.3-flash:cloud", "messages": []},
        )

    assert recorder.anthropic == ["glm-5.3-flash:cloud"]


def test_direct_failures_do_not_move_the_quota_driven_mode() -> None:
    """A busy pinned model must not reroute everyone else's traffic.

    名指しされたモデルが混雑していても、他のトラフィックの行き先を変えてはならない。
    """
    recorder = _Recorder()
    settings = _settings()

    def failing_ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS_PAYLOAD)
        if request.url.path == "/api/usage":
            return httpx.Response(404)
        return httpx.Response(503, json={"error": "busy"})

    backends = ProxyBackends(
        settings,
        anthropic_transport=httpx.MockTransport(recorder.anthropic_handler),
        ollama_transport=httpx.MockTransport(failing_ollama),
    )
    app = create_app(settings, backends)
    with TestClient(app) as client:
        for _ in range(settings.routing.ollama_failure_threshold + 2):
            client.post(
                "/v1/messages",
                json={"model": "glm-5.3-flash:cloud", "messages": [], "max_tokens": 1},
            )

        status = client.get("/_spillway/status").json()

    stats = backends.ollama_stats
    assert stats.consecutive_failures == 0
    assert stats.requests_sent == 0
    assert status["direct_models"]["requests_failed"] == (
        settings.routing.ollama_failure_threshold + 2
    )


def test_status_reports_the_discovered_models() -> None:
    recorder = _Recorder()
    with _client(_settings(), recorder) as client:
        status = client.get("/_spillway/status").json()

    assert status["direct_models"]["enabled"] is True
    assert status["direct_models"]["models"] == [
        "gemma4:31b",
        "glm-5.3-flash",
        "gpt-oss:120b",
    ]


# --- picker sync ----------------------------------------------------------


def test_picker_options_always_carry_behaves_as() -> None:
    config = _settings().direct_models.picker
    options = build_picker_options(["glm-5.3-flash", "gemma4:31b"], config)

    assert [option["model"] for option in options] == ["glm-5.3-flash:cloud", "gemma4:31b-cloud"]
    # Without behavesAs, Claude Code drops the row for a model it does not know.
    # behavesAs が無いと、Claude Codeは未知のモデルの行を表示しない。
    assert all(option["behavesAs"] == "claude-sonnet-5" for option in options)


def test_picker_options_honour_include_exclude_and_overrides() -> None:
    settings = _settings(
        picker={
            "include": ["glm-*", "gemma4*"],
            "exclude": ["gemma4*"],
            "overrides": [
                {"match": "glm-*", "label": "GLM", "behaves_as": "claude-opus-5"},
            ],
        }
    )
    options = build_picker_options(["glm-5.3-flash", "gemma4:31b", "gpt-oss:120b"], settings.direct_models.picker)

    assert options == [
        {
            "model": "glm-5.3-flash:cloud",
            "label": "GLM",
            "description": "Ollama Cloud · routed directly by claude-spillway",
            "behavesAs": "claude-opus-5",
        }
    ]


def test_extended_context_marker_round_trips() -> None:
    """The row claims 1M to Claude Code; the marker never reaches Ollama.

    行はClaude Codeに対して1Mを主張するが、その印はOllamaまで届かない。
    """
    settings = _settings(picker={"extended_context": ["glm-*"]})
    options = build_picker_options(["glm-5.3-flash", "gemma4:31b"], settings.direct_models.picker)

    assert [option["model"] for option in options] == [
        "glm-5.3-flash:cloud[1m]",
        "gemma4:31b-cloud",
    ]

    registry = DirectModelRegistry(settings.direct_models)
    registry.update(["glm-5.3-flash"])
    assert registry.is_direct("glm-5.3-flash:cloud[1m]")
    assert registry.outbound_name("glm-5.3-flash:cloud[1m]") == "glm-5.3-flash:cloud"


def test_sync_model_picker_preserves_the_rest_of_the_settings(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps({"theme": "dark", "model": "opus[1m]", "modelPicker": {"options": []}}),
        encoding="utf-8",
    )
    config = _settings(picker={"settings_path": str(target)}).direct_models.picker

    path, options, backup = sync_model_picker(["glm-5.3-flash"], config)

    written = json.loads(target.read_text(encoding="utf-8"))
    assert path == target
    assert written["theme"] == "dark"
    # The model the user picked last is Claude Code's to manage, not ours.
    # 最後に選択されたモデルはClaude Codeが管理するものであり、こちらは触らない。
    assert written["model"] == "opus[1m]"
    assert written[SETTINGS_PICKER_KEY]["options"] == options
    assert written[SETTINGS_PICKER_KEY]["replaceBuiltInOptions"] is False
    assert backup is not None and json.loads(backup.read_text(encoding="utf-8"))["theme"] == "dark"


def test_sync_model_picker_dry_run_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    config = _settings(picker={"settings_path": str(target)}).direct_models.picker

    _, options, backup = sync_model_picker(["glm-5.3-flash"], config, dry_run=True)

    assert options
    assert backup is None
    assert json.loads(target.read_text(encoding="utf-8")) == {"theme": "dark"}


def test_sync_model_picker_creates_a_missing_settings_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "settings.json"
    config = _settings(picker={"settings_path": str(target)}).direct_models.picker

    _, options, backup = sync_model_picker(["glm-5.3-flash"], config)

    assert backup is None
    assert json.loads(target.read_text(encoding="utf-8"))[SETTINGS_PICKER_KEY]["options"] == options


# --- interactive selection ------------------------------------------------


def test_toggle_picks_numbers_and_all_none() -> None:
    selected: set[int] = {0}
    assert toggle_picks(selected, ["1", "3"], 5)
    # Toggling 1 again removes row 1; 3 was added.
    # 1 をもう一度入れると行1が外れ、3 は追加された側。
    assert selected == {2}
    assert toggle_picks(selected, ["a"], 5)
    assert selected == {0, 1, 2, 3, 4}
    assert toggle_picks(selected, ["N"], 5)
    assert selected == set()


def test_toggle_picks_keeps_changes_before_a_bad_token() -> None:
    selected: set[int] = {0}
    # Out-of-range and non-numeric tokens are rejected whole-token, and the
    # changes made before them survive, matching the warning the loop prints.
    # 範囲外・非数値のトークンはそのトークンだけを捨て、それまでの変更は残る。
    # これはループが表示する警告文言と対応している。
    assert not toggle_picks(selected, ["2", "0"], 5)
    assert selected == {0, 1}
    assert not toggle_picks(selected, ["x"], 5)
    assert selected == {0, 1}


def test_split_patterns_flattens_repeats_and_commas() -> None:
    assert _split_patterns(None) is None
    assert _split_patterns(["glm-*"]) == ["glm-*"]
    assert _split_patterns(["glm-*,gpt-*", " kimi-* "]) == ["glm-*", "gpt-*", "kimi-*"]


def test_registered_picker_models_reads_back_what_was_written(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    config = _settings(picker={"settings_path": str(target)}).direct_models.picker
    sync_model_picker(["glm-5.3-flash", "gemma4:31b"], config)

    written = json.loads(target.read_text(encoding="utf-8"))
    # Suffixes and the [1m] marker are all normalised away, so a row can be
    # matched back to the discovered base name.
    # サフィックスも [1m] 印も正規化で取り除かれるため、行を取得時の素の名前へ
    # 対応付けられる。
    assert registered_picker_models(written) == {"glm-5.3-flash", "gemma4:31b"}
    assert registered_picker_models({}) == set()
    assert registered_picker_models({SETTINGS_PICKER_KEY: {"options": "broken"}}) == set()
