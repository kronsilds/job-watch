"""Connexion « Sign in with Claude » (OAuth 2.0 Authorization Code + PKCE).

Permet d'utiliser l'API Anthropic avec un **compte Claude** (Pro/Max/Team) :
l'utilisateur s'authentifie dans son navigateur, l'application reçoit un
``access_token`` (bearer) et un ``refresh_token`` — aucun ``x-api-key``.

Flow public (celui de Claude Code, client public sans secret) :

- autorisation : ``https://claude.ai/oauth/authorize`` (PKCE S256) ;
- échange / refresh : ``https://console.anthropic.com/v1/oauth/token`` ;
- appels API : ``Authorization: Bearer <access_token>`` +
  ``anthropic-beta: oauth-2025-04-20`` (requis pour débloquer le bearer).

Les tokens sont stockés localement dans ``data/claude_oauth_tokens.json``
(répertoire ``/data/`` ignoré par git). Le refresh tourne en **rotation** :
chaque rafraîchissement renouvelle le refresh_token et invalide l'ancien.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

load_dotenv()

# Client public de Claude Code (aucun secret ; PKCE le remplace).
DEFAULT_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
DEFAULT_SCOPES = "user:inference user:profile"
# Beta header requis sur les appels API en bearer et sur les refresh_token.
OAUTH_BETA = "oauth-2025-04-20"
# Rafraîchir l'access token 2 min avant son expiration (pratique du SDK Anthropic).
REFRESH_MARGIN = 120
# Un flow non abouti expire au bout de 15 minutes.
FLOW_TTL = 900

_ROOT = Path(__file__).resolve().parents[2]
# state -> {"verifier", "redirect_uri", "created_at"} (mémoire locale, mono-process).
_PENDING: dict[str, dict] = {}


def client_id() -> str:
    return os.getenv("ANTHROPIC_OAUTH_CLIENT_ID") or DEFAULT_CLIENT_ID


def authorize_endpoint() -> str:
    return os.getenv("ANTHROPIC_OAUTH_AUTHORIZE_URL") or AUTHORIZE_URL


def token_endpoint() -> str:
    return os.getenv("ANTHROPIC_OAUTH_TOKEN_URL") or TOKEN_URL


def scopes() -> str:
    return os.getenv("ANTHROPIC_OAUTH_SCOPES") or DEFAULT_SCOPES


def beta_header() -> str:
    """Beta flag envoyée avec le bearer (surchargeable si Anthropic l'évolue)."""
    return os.getenv("ANTHROPIC_OAUTH_BETA") or OAUTH_BETA


def redirect_uri() -> str:
    """URI de rappel — le chemin ``/callback`` est servi par l'application.

    Le client Claude Code n'autorise que des rappels sur localhost ; le chemin
    par défaut suit sa convention. Surchargeable via ``ANTHROPIC_OAUTH_REDIRECT_URI``.
    """
    return os.getenv("ANTHROPIC_OAUTH_REDIRECT_URI") or "http://localhost:8000/callback"


def tokens_file() -> Path:
    return Path(os.getenv("ANTHROPIC_OAUTH_TOKENS_FILE")
                or _ROOT / "data" / "claude_oauth_tokens.json")


def _pkce() -> tuple[str, str]:
    """Retourne (code_verifier, code_challenge) — défi S256 sans padding."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def start_login(state: str = "") -> dict:
    """Prépare l'URL d'autorisation (PKCE) et mémorise le flow en cours."""
    now = time.time()
    for key in [k for k, v in _PENDING.items() if now - v["created_at"] > FLOW_TTL]:
        _PENDING.pop(key, None)
    state = state or secrets.token_urlsafe(32)
    verifier, challenge = _pkce()
    _PENDING[state] = {"verifier": verifier, "redirect_uri": redirect_uri(),
                       "created_at": now}
    params = {
        "code": "true",
        "client_id": client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "scope": scopes(),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return {"provider": "claude", "state": state,
            "authorization_url": authorize_endpoint() + "?" + urlencode(params)}


def _post_token(payload: dict) -> dict:
    headers = {"content-type": "application/json",
               "anthropic-beta": beta_header()}
    with httpx.Client(timeout=30) as client:
        r = client.post(token_endpoint(), headers=headers, json=payload)
        try:
            r.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"Requête OAuth refusée ({exc}) : {r.text[:300]}")
        data = r.json()
    if not data.get("access_token"):
        raise RuntimeError("Réponse OAuth invalide (access_token absent)")
    return data


def _load() -> dict:
    try:
        data = json.loads(tokens_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    previous = _load()
    now = time.time()
    tokens = {
        "access_token": data["access_token"],
        # Rotation : le refresh renvoie un nouveau refresh_token, l'ancien expire.
        "refresh_token": data.get("refresh_token") or previous.get("refresh_token", ""),
        "expires_at": now + float(data.get("expires_in") or 0) - REFRESH_MARGIN,
        "scope": data.get("scope") or previous.get("scope", ""),
        "account": data.get("account") or previous.get("account") or {},
        "organization": data.get("organization") or previous.get("organization") or {},
        "saved_at": now,
    }
    path = tokens_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    try:
        path.chmod(0o600)  # fichier de secrets
    except OSError:
        pass  # Windows


def connected() -> bool:
    tokens = _load()
    return bool(tokens.get("access_token") or tokens.get("refresh_token"))


def exchange_code(code: str, state: str) -> dict:
    """Échange le code d'autorisation contre access + refresh tokens."""
    flow = _PENDING.pop(state, None)
    if not flow:
        raise RuntimeError("État OAuth inconnu ou expiré : relancez la connexion.")
    # Certains rappels collent l'état dans le fragment du code ("code#state").
    clean_code, _, fragment = str(code).partition("#")
    data = _post_token({
        "grant_type": "authorization_code",
        "client_id": client_id(),
        "code": clean_code.strip(),
        "state": fragment or state,
        "redirect_uri": flow["redirect_uri"],
        "code_verifier": flow["verifier"],
    })
    _save(data)
    return status()


def refresh_tokens() -> str:
    """Rafraîchit l'access token (rotation du refresh token) et le renvoie."""
    refresh = _load().get("refresh_token")
    if not refresh:
        raise RuntimeError("Aucun refresh token : reconnectez le compte Claude.")
    data = _post_token({"grant_type": "refresh_token",
                        "client_id": client_id(),
                        "refresh_token": refresh})
    _save(data)
    return data["access_token"]


def get_access_token(force_refresh: bool = False) -> str:
    """Access token valide : renvoyé tel quel ou rafraîchi si expiré/imminent."""
    tokens = _load()
    if (not force_refresh and tokens.get("access_token")
            and float(tokens.get("expires_at") or 0) > time.time()):
        return tokens["access_token"]
    return refresh_tokens()


def bearer_token() -> str:
    """Bearer courant ou '' si aucun compte Claude n'est connecté."""
    return get_access_token() if connected() else ""


def status() -> dict:
    tokens = _load()
    if not (tokens.get("access_token") or tokens.get("refresh_token")):
        return {"provider": "claude", "connected": False}
    account = tokens.get("account") or {}
    return {"provider": "claude", "connected": True,
            "email": account.get("email_address", ""),
            "expires_at": tokens.get("expires_at"),
            "expired": float(tokens.get("expires_at") or 0) <= time.time()}


def logout() -> dict:
    try:
        tokens_file().unlink()
    except OSError:
        pass
    _PENDING.clear()
    return {"provider": "claude", "connected": False}