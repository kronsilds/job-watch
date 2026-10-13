"""Multi-utilisateurs : comptes, annonces partagées, notations personnelles.

Contrat (option B) :
- ``jobs`` = catalogue partagé (dédupliqué par ``external_id``) ;
- la note / décision / confiance qui filtre l'affichage vit dans
  ``job_ratings`` (une ligne par ``(user_id, job_id)``) ;
- profil, candidatures, événements et audits sont isolés par ``user_id``.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base
from app.models import Job, Profile


def _client(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBWATCH_SINGLE_USER", raising=False)
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "multi.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "db", lambda: SL())
    return TestClient(main.app), SL


def _register(c, username, password="secret123"):
    r = c.post("/api/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(c, token):
    return {"Authorization": f"Bearer {token}"}


def _seed_job_and_profile(sl):
    d = sl()
    d.add(Job(external_id="shared-1", title="Data Analyst", company="Acme",
              skills=["SQL"], description="SQL"))
    d.commit()


def test_register_login_and_me(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    assert c.get("/api/jobs").status_code == 401
    alice = _register(c, "alice")
    assert alice["username"] == "alice" and alice["token"]
    me = c.get("/api/auth/me", headers=_auth(c, alice["token"])).json()
    assert me["user"]["username"] == "alice"
    # Login renouvelle le token ; l'ancien reste valide, le logout le révoque.
    login = c.post("/api/auth/login",
                   json={"username": "alice", "password": "secret123"}).json()
    assert login["token"] != alice["token"]
    assert c.post("/api/auth/logout",
                  headers=_auth(c, login["token"])).status_code == 200
    assert c.get("/api/jobs", headers=_auth(c, login["token"])).status_code == 401
    assert c.get("/api/jobs", headers=_auth(c, alice["token"])).status_code == 200
    assert c.post("/api/auth/login",
                  json={"username": "alice", "password": "nope"}).status_code == 401


def test_shared_jobs_but_personal_ratings(tmp_path, monkeypatch):
    c, sl = _client(tmp_path, monkeypatch)
    alice = _register(c, "alice")
    bob = _register(c, "bob")
    ha, hb = _auth(c, alice["token"]), _auth(c, bob["token"])
    _seed_job_and_profile(sl)
    # Deux profils validés différents → deux notations différentes, une seule annonce.
    for h, skill in ((ha, "SQL"), (hb, "Boulangerie")):
        r = c.put("/api/profile", headers=h, json={
            "name": "X", "validate_profile": True, "source_text": "",
            "data": {"skills": [skill], "jobs": ["Data Analyst"]}})
        assert r.status_code == 200, r.text
    ja = c.get("/api/jobs", headers=ha).json()
    jb = c.get("/api/jobs", headers=hb).json()
    assert len(ja) == len(jb) == 1 and ja[0]["id"] == jb[0]["id"]
    assert (ja[0]["score"] or 0) > (jb[0]["score"] or 0)
    # Décision personnelle : Alice retient, Bob ne voit rien changer.
    assert c.post("/api/jobs/1/decision", headers=ha,
                  params={"value": "retained"}).status_code == 200
    assert c.get("/api/jobs", headers=ha,
                 params={"decision": "retained"}).json()[0]["id"] == 1
    assert c.get("/api/jobs", headers=hb,
                 params={"decision": "retained"}).json() == []
    assert c.get("/api/jobs", headers=hb).json()[0]["decision"] == "new"


def test_applications_and_audit_are_isolated(tmp_path, monkeypatch):
    c, sl = _client(tmp_path, monkeypatch)
    alice = _register(c, "alice")
    bob = _register(c, "bob")
    ha, hb = _auth(c, alice["token"]), _auth(c, bob["token"])
    _seed_job_and_profile(sl)
    for h in (ha, hb):
        c.put("/api/profile", headers=h, json={
            "name": "X", "validate_profile": True, "source_text": "",
            "data": {"skills": ["SQL"], "jobs": ["Data Analyst"]}})
    app_id = c.post("/api/applications", headers=ha,
                    params={"job_id": 1}).json()["id"]
    assert c.get(f"/api/applications/{app_id}", headers=hb).status_code == 404
    assert c.get("/api/applications", headers=hb).json() == []
    assert len(c.get("/api/applications", headers=ha).json()) == 1
    assert c.get("/api/kanban", headers=hb).json()["cards"] == []
    assert len(c.get("/api/kanban", headers=ha).json()["cards"]) == 1
    # L'audit suit la même isolation (l'action d'Alice invisible à Bob).
    assert c.get("/api/audit", headers=hb).json() == []
    kinds = [e["kind"] for e in c.get("/api/audit", headers=ha).json()]
    assert "application_created" in kinds


def test_legacy_single_user_mode_keeps_working(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_SINGLE_USER", "1")
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "legacy.db"),
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "db", lambda: SL())
    c = TestClient(main.app)
    d = SL()
    d.add(Profile(status="validated", data={"skills": ["SQL"]}))
    d.add(Job(external_id="x", title="Analyste", company="Acme", skills=["SQL"]))
    d.commit()
    assert c.get("/api/jobs").status_code == 200
    assert c.post("/api/applications", params={"job_id": 1}).status_code == 200
