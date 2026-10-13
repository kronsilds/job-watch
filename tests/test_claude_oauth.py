"""Connexion OAuth Claude (Anthropic) — flow Authorization Code + PKCE.

Aucun appel réseau : l'échange/refresh de tokens est simulé (``httpx``
monkeypatché) et le stockage est détourné vers un fichier temporaire.
"""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app import llm
from app.llm import AnthropicProvider, effective_provider
from app.providers import claude_oauth

_ENV_VARS = ("LLM_PROVIDER", "LLM_API_KEY", "ANTHROPIC_API_KEY",
             "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
             "LLM_MODEL", "LLM_MODEL_HEAVY", "LLM_MODEL_LIGHT",
             "ANTHROPIC_BASE_URL", "LLM_BASE_URL", "LLM_MAX_TOKENS",
             "ANTHROPIC_OAUTH_TOKENS_FILE", "ANTHROPIC_OAUTH_REDIRECT_URI",
             "ANTHROPIC_OAUTH_BETA", "ANTHROPIC_OAUTH_CLIENT_ID")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKENS_FILE",
                       str(tmp_path / "tokens.json"))
    claude_oauth._PENDING.clear()
    yield
    claude_oauth._PENDING.clear()


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")

    def json(self):
        return self._payload


def _token_payload(**overrides):
    payload = {"token_type": "Bearer", "access_token": "sk-ant-oat01-new",
               "expires_in": 28800, "refresh_token": "sk-ant-ort01-new",
               "scope": "user:inference user:profile",
               "account": {"email_address": "user@example.com"}}
    payload.update(overrides)
    return payload


def fake_token_http(monkeypatch, payload=None):
    """Remplace ``httpx.Client`` (token endpoint) et capture la requête."""
    captured = {}
    body = payload or _token_payload()

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse(body)

    monkeypatch.setattr(claude_oauth.httpx, "Client", FakeClient)
    return captured


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# --- URL d'autorisation (PKCE) -----------------------------------------------

def test_start_login_builds_pkce_url():
    info = claude_oauth.start_login()
    assert info["provider"] == "claude"
    url = urlparse(info["authorization_url"])
    q = parse_qs(url.query)
    assert url.scheme == "https" and url.netloc == "claude.ai"
    assert q["client_id"] == [claude_oauth.DEFAULT_CLIENT_ID]
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["user:inference user:profile"]
    assert q["redirect_uri"] == ["http://localhost:8000/callback"]
    assert "client_secret" not in q  # client public : PKCE, jamais de secret
    # le défi correspond au verifier mémorisé pour l'échange
    flow = claude_oauth._PENDING[info["state"]]
    assert q["code_challenge"] == [_s256(flow["verifier"])]


def test_start_login_respects_redirect_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_OAUTH_REDIRECT_URI",
                       "http://localhost:9999/callback")
    info = claude_oauth.start_login()
    q = parse_qs(urlparse(info["authorization_url"]).query)
    assert q["redirect_uri"] == ["http://localhost:9999/callback"]


# --- échange du code ----------------------------------------------------------

def test_exchange_code_saves_tokens(monkeypatch):
    captured = fake_token_http(monkeypatch)
    info = claude_oauth.start_login()
    status = claude_oauth.exchange_code("auth-code-123", info["state"])
    assert status["connected"] is True
    assert status["email"] == "user@example.com"
    body = captured["json"]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "auth-code-123"
    assert body["client_id"] == claude_oauth.DEFAULT_CLIENT_ID
    assert body["redirect_uri"] == "http://localhost:8000/callback"
    # le code_verifier envoyé correspond au challenge de l'URL d'autorisation
    q = parse_qs(urlparse(info["authorization_url"]).query)
    assert q["code_challenge"] == [_s256(body["code_verifier"])]
    saved = json.loads(claude_oauth.tokens_file().read_text(encoding="utf-8"))
    assert saved["access_token"] == "sk-ant-oat01-new"
    assert saved["refresh_token"] == "sk-ant-ort01-new"
    assert saved["expires_at"] == pytest.approx(time.time() + 28800 - 120, abs=30)


def test_exchange_code_rejects_unknown_state(monkeypatch):
    fake_token_http(monkeypatch)
    with pytest.raises(RuntimeError):
        claude_oauth.exchange_code("auth-code-123", "state-inconnu")


# --- refresh (rotation) et durée de vie --------------------------------------

def test_get_access_token_returns_valid_token_without_refresh(monkeypatch):
    captured = fake_token_http(monkeypatch)
    claude_oauth.tokens_file().write_text(json.dumps({
        "access_token": "sk-ant-oat01-valid", "refresh_token": "r",
        "expires_at": time.time() + 3600}), encoding="utf-8")
    assert claude_oauth.get_access_token() == "sk-ant-oat01-valid"
    assert captured == {}  # aucun appel réseau


def test_refresh_rotates_tokens_and_sends_beta_header(monkeypatch):
    captured = fake_token_http(monkeypatch, payload=_token_payload(
        access_token="sk-ant-oat01-2", refresh_token="sk-ant-ort01-2"))
    claude_oauth.tokens_file().write_text(json.dumps({
        "access_token": "sk-ant-oat01-old", "refresh_token": "sk-ant-ort01-old",
        "expires_at": time.time() - 10}), encoding="utf-8")  # expiré
    token = claude_oauth.get_access_token()
    assert token == "sk-ant-oat01-2"
    body = captured["json"]
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "sk-ant-ort01-old"
    assert captured["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    saved = json.loads(claude_oauth.tokens_file().read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "sk-ant-ort01-2"  # rotation stockée


def test_logout_removes_tokens(monkeypatch):
    fake_token_http(monkeypatch)
    info = claude_oauth.start_login()
    claude_oauth.exchange_code("code", info["state"])
    assert claude_oauth.connected() is True
    assert claude_oauth.logout()["connected"] is False
    assert claude_oauth.connected() is False


# --- AnthropicProvider en mode bearer (sans x-api-key) -----------------------

def _fake_messages_http(monkeypatch, responses):
    """httpx factice pour la Messages API ; `responses` = liste de FakeResponse."""
    captured = []

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            captured.append({"url": url, "headers": headers, "json": json})
            return responses.pop(0)

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    return captured


def _save_oauth_tokens(access="sk-ant-oat01-valid", refresh="r"):
    claude_oauth.tokens_file().write_text(json.dumps({
        "access_token": access, "refresh_token": refresh,
        "expires_at": time.time() + 3600}), encoding="utf-8")


def test_anthropic_provider_uses_oauth_bearer_without_api_key(monkeypatch):
    _save_oauth_tokens()
    calls = _fake_messages_http(monkeypatch, [FakeResponse(
        {"content": [{"type": "text", "text": '{"summary": "ok"}'}]})])
    p = AnthropicProvider()
    p._sdk_client = lambda: None  # force le chemin HTTP
    assert p._bearer is True
    assert p._complete("sys", "user", "claude-haiku-5-5", 0.2) == {"summary": "ok"}
    headers = calls[0]["headers"]
    assert headers["Authorization"] == "Bearer sk-ant-oat01-valid"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-api-key" not in headers


def test_anthropic_provider_accepts_static_bearer(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-static")
    calls = _fake_messages_http(monkeypatch, [FakeResponse(
        {"content": [{"type": "text", "text": '{"summary": "ok"}'}]})])
    p = AnthropicProvider()
    p._sdk_client = lambda: None
    assert p._bearer is True
    p._complete("sys", "user", "claude-haiku-5-5", 0.2)
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ant-oat01-static"


def test_api_key_shadows_bearer(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-static")
    p = AnthropicProvider()
    assert p._bearer is False
    headers = p._headers()
    assert headers["x-api-key"] == "sk-ant-api"
    assert "Authorization" not in headers


def test_expired_oauth_token_refreshes_and_retries(monkeypatch):
    _save_oauth_tokens(access="sk-ant-oat01-expired")
    seen = {"token_calls": 0, "messages": 0}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            if url.endswith("/oauth/token"):
                seen["token_calls"] += 1
                return FakeResponse(_token_payload(
                    access_token="sk-ant-oat01-fresh", refresh_token="r2"))
            seen["messages"] += 1
            if seen["messages"] == 1:
                return FakeResponse({"error": "unauthorized"}, status_code=401)
            return FakeResponse(
                {"content": [{"type": "text", "text": '{"summary": "ok"}'}]})

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    monkeypatch.setattr(claude_oauth.httpx, "Client", FakeClient)
    p = AnthropicProvider()
    p._sdk_client = lambda: None
    assert p._complete("sys", "user", "claude-haiku-5-5", 0.2) == {"summary": "ok"}
    assert seen == {"token_calls": 1, "messages": 2}  # refresh puis retry


# --- routes HTTP ---------------------------------------------------------------

def test_claude_routes_not_swallowed_by_generic_provider_route():
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    r = client.get("/api/oauth/claude/authorize")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "claude" and "authorization_url" in body
    assert client.get("/api/oauth/claude/status").json()["connected"] is False


def test_callback_endpoint_exchanges_code(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    fake_token_http(monkeypatch)
    client = TestClient(main.app)
    info = claude_oauth.start_login()
    r = client.get(f"/callback?code=auth-code-123&state={info['state']}")
    assert r.status_code == 200 and "connecté" in r.text
    assert claude_oauth.status()["email"] == "user@example.com"
    # alias historique du rappel OAuth
    r2 = client.get("/api/oauth/callback?code=auth-code-123&state=state-inconnu")
    assert r2.status_code == 502  # état inconnu -> échec explicite, pas de crash


def test_callback_endpoint_reports_provider_error():
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    r = client.get("/callback?error=access_denied")
    assert r.status_code == 400 and "annulée" in r.text


def test_logout_endpoint_clears_tokens():
    from fastapi.testclient import TestClient
    from app import main
    _save_oauth_tokens()
    client = TestClient(main.app)
    assert client.get("/api/oauth/claude/status").json()["connected"] is True
    assert client.post("/api/oauth/claude/logout").json()["connected"] is False
    assert claude_oauth.connected() is False