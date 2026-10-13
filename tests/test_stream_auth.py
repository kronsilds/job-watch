"""Le token est envoyé sur les appels streamés (FormData import CV, campagnes…).

Non-régression du bug « connecté mais 401 sur l'analyse CV » : ``streamPost``
n'injectait pas le header ``Authorization`` (contrairement à ``api``), donc
tout upload/requête streamée échouait même avec un token valide en
``localStorage``.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("JOBWATCH_SINGLE_USER", raising=False)
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "stream-auth.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "db", lambda: SL())
    return TestClient(main.app)


def test_cv_import_stream_requires_token_but_accepts_it(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    cv = {"file": ("cv.txt", "Dev Python SQL".encode("utf-8"), "text/plain")}
    assert c.post("/api/cv/import/stream", files=cv).status_code == 401
    token = c.post("/api/auth/register",
                   json={"username": "oliv", "password": "secret123"}).json()["token"]
    r = c.post("/api/cv/import/stream", files=cv,
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert '"type": "done"' in r.text
