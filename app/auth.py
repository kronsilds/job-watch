"""Comptes locaux job-watch : mot de passe haché (PBKDF2 stdlib) + token Bearer.

Aucune dépendance ajoutée : ``hashlib``/``secrets`` de la stdlib suffisent pour
un usage local/familial. Les tokens sont opaques, stockés en base (table
``auth_tokens``) et révocables (logout).
"""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets

def hash_password(password: str, salt: str | None = None) -> str:
    if not password or len(password) < 4:
        raise ValueError("Mot de passe trop court (4 caractères minimum).")
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2-sha256$200000${salt}${dk.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, hexhash = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters))
        return hmac.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False

def new_token() -> str:
    return secrets.token_urlsafe(32)

def normalize_username(username: str) -> str:
    u = (username or "").strip().lower()
    if len(u) < 2 or len(u) > 64:
        raise ValueError("Nom d'utilisateur invalide (2 à 64 caractères).")
    if any(c.isspace() for c in u):
        raise ValueError("Nom d'utilisateur sans espaces.")
    return u

def auth_enabled() -> bool:
    """Authentification active sauf mode mono-utilisateur explicite.

    ``JOBWATCH_SINGLE_USER=1`` restaure le comportement historique (aucun
    compte requis, données legacy ``user_id IS NULL`` visibles). Par défaut,
    l'API exige un compte.
    """
    return os.getenv("JOBWATCH_SINGLE_USER", "") not in ("1", "true", "yes")
