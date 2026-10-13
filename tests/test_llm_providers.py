"""Sélection du fournisseur LLM et transports distants (OpenAI / Anthropic).

Aucun appel réseau : les transports sont testés avec des clients factices
(SDK ``anthropic`` simulé, HTTP monkeypatché) et la sélection avec des
variables d'environnement isolées de la vraie configuration locale.
"""

import pytest

from app import llm
from app.llm import (
    AnthropicProvider, DeterministicProvider, FallbackProvider,
    OpenAICompatibleProvider, RemoteProvider, _model, _parse_json,
    effective_provider, get_provider,
)
from app.profile_analysis import _llm_configured

_ENV_VARS = ("LLM_PROVIDER", "LLM_API_KEY", "ANTHROPIC_API_KEY",
             "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
             "LLM_MODEL", "LLM_MODEL_HEAVY", "LLM_MODEL_LIGHT",
             "ANTHROPIC_BASE_URL", "LLM_BASE_URL", "LLM_MAX_TOKENS",
             "ANTHROPIC_OAUTH_TOKENS_FILE", "ANTHROPIC_OAUTH_REDIRECT_URI",
             "ANTHROPIC_OAUTH_BETA", "ANTHROPIC_OAUTH_CLIENT_ID")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isole chaque test de la configuration réelle (.env chargé au import)."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Aucun compte OAuth Claude réel ne doit fuiter dans les tests.
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKENS_FILE",
                       str(tmp_path / "no-tokens.json"))
    yield


# --- sélection du fournisseur -------------------------------------------------

def test_effective_provider_local_by_default():
    assert effective_provider() == "local"


def test_effective_provider_auto_detection(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert effective_provider() == "anthropic"
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    assert effective_provider() == "compatible"  # compat : la clé LLM garde la main


def test_effective_provider_explicit_values(monkeypatch):
    for value in ("anthropic", "claude", "openrouter", "openai",
                  "compatible", "local"):
        monkeypatch.setenv("LLM_PROVIDER", value)
        assert effective_provider() == value


def test_get_provider_anthropic_uses_fallback_chain(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    p = get_provider()
    assert isinstance(p, FallbackProvider)
    assert isinstance(p.primary, AnthropicProvider)
    assert isinstance(p.fallback, DeterministicProvider)
    assert p.primary.name == "claude"


def test_get_provider_anthropic_accepts_llm_api_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-via-llm")
    p = get_provider()
    assert isinstance(p.primary, AnthropicProvider)


def test_get_provider_openai_compatible_unchanged(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    p = get_provider()
    assert isinstance(p.primary, OpenAICompatibleProvider)
    monkeypatch.setenv("LLM_PROVIDER", "local")
    assert isinstance(get_provider(), DeterministicProvider)


# --- modèles par défaut -------------------------------------------------------

def test_model_defaults_follow_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert _model(True) == "claude-sonnet-5-5"
    assert _model(False) == "claude-haiku-5-5"
    monkeypatch.setenv("LLM_MODEL_HEAVY", "claude-opus-5-5")
    assert _model(True) == "claude-opus-5-5"
    monkeypatch.setenv("LLM_MODEL", "claude-fable-5-1")
    assert _model(False) == "claude-fable-5-1"  # LLM_MODEL avant le défaut


def test_model_defaults_for_openai_compatible(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    assert _model(True) == "openai/gpt-4o"
    assert _model(False) == "openai/gpt-4o-mini"


# --- parsing JSON -------------------------------------------------------------

def test_parse_json_tolerates_fences_and_noise():
    assert _parse_json('{"a": 1}') == {"a": 1}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('Voici le résultat : {"a": 1}') == {"a": 1}
    with pytest.raises(RuntimeError):
        _parse_json("pas du json du tout")
    with pytest.raises(RuntimeError):
        _parse_json('["une liste"]')  # un objet JSON est attendu


# --- transport Anthropic ------------------------------------------------------

class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessages:
    """SDK ``anthropic`` simulé : enregistre les appels, renvoie du JSON."""

    def __init__(self, text='{"summary": "sdk"}'):
        self.calls = []
        self._text = text

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return type("R", (), {"content": [_FakeBlock(self._text)]})()


def test_anthropic_sdk_path_parses_text_blocks(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    p = AnthropicProvider()
    fake = type("FakeSDK", (), {})()
    fake.messages = _FakeMessages()
    p.client = fake
    out = p._complete("sys", "user", "claude-sonnet-5-5", 0.1)
    assert out == {"summary": "sdk"}
    call = fake.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["max_tokens"] == 2048
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "user"}]


def test_anthropic_http_fallback_posts_messages_api(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.test")
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": '{"summary": "ok"}'}]}

    class FakeClient:
        def __init__(self, timeout=None):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    p = AnthropicProvider()
    p.client = None  # force le repli HTTP (SDK absent ou en échec)
    out = p._complete("sys", "user", "claude-haiku-5-5", 0.2)
    assert out == {"summary": "ok"}
    assert captured["url"] == "https://api.test/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["json"]["model"] == "claude-haiku-5-5"
    assert captured["json"]["system"] == "sys"
    assert captured["json"]["messages"] == [{"role": "user", "content": "user"}]
    assert captured["json"]["max_tokens"] == 4096


def test_anthropic_sdk_failure_falls_back_to_http(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": '{"summary": "http"}'}]}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            calls.append(url)
            return FakeResponse()

    class BoomMessages:
        def create(self, **kwargs):
            raise RuntimeError("api down")

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    p = AnthropicProvider()
    fake = type("FakeSDK", (), {})()
    fake.messages = BoomMessages()
    p.client = fake
    out = p._complete("sys", "user", "claude-haiku-5-5", 0.2)
    assert out == {"summary": "http"}
    assert calls == ["https://api.anthropic.com/v1/messages"]


def test_anthropic_tasks_use_tier_models(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    p = AnthropicProvider()
    assert isinstance(p, RemoteProvider)  # couche tâches partagée
    seen = []

    def fake_complete(system, user, model, temperature):
        seen.append(model)
        return {"summary": "s"}

    p._complete = fake_complete
    assert p.summarize_market({"jobs_count": 1}) == {"summary": "s"}
    assert seen == ["claude-sonnet-5-5"]  # tâche heavy


def test_llm_configured_recognizes_anthropic(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert _llm_configured() is True
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert _llm_configured() is True
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert _llm_configured() is False


def test_effective_provider_detects_bearer_and_oauth_tokens(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-x")
    assert effective_provider() == "anthropic"
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-y")
    assert effective_provider() == "anthropic"
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    assert effective_provider() == "local"
    from app.providers import claude_oauth
    claude_oauth.tokens_file().write_text('{"access_token": "a"}', encoding="utf-8")
    assert effective_provider() == "anthropic"  # compte OAuth connecté