import os
import pytest


@pytest.fixture(autouse=True)
def _no_remote_scoring(monkeypatch):
    # La suite ne doit jamais appeler Jev (OpenRouter), même si une clé est dans `.env`.
    monkeypatch.setenv("JOBWATCH_SCORER", "heuristic")


@pytest.fixture(autouse=True)
def _single_user_unless_multiuser(monkeypatch, request):
    # Les tests historiques ont été écrits avant les comptes : ils tournent en
    # mode mono-utilisateur, sauf la suite dédiée `test_multiuser.py`.
    if "multiuser" not in (request.node.module.__name__ if request.node.module else ""):
        monkeypatch.setenv("JOBWATCH_SINGLE_USER", "1")
    else:
        monkeypatch.delenv("JOBWATCH_SINGLE_USER", raising=False)
