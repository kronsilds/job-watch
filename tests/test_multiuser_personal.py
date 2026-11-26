"""Multi-comptes : l'annonce partagée ne porte aucune évaluation dépendant d'un profil.

Tout ce qui dépend du profil (note, confiance, décision, explication, filtres,
pré-note, cache Jev) vit dans ``job_ratings`` ; un compte ne voit jamais
l'évaluation calculée pour un autre.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import jev, main
from app.db import Base
from app.models import Job, JobRating, Profile, User
from app.scoring import score_all, score_job_jev

CDI_PROFILE = {"skills": ["SQL"], "jobs": ["Data Analyst"], "locations": ["Paris"],
               "contracts": ["CDI"], "years_experience": 4}
STUDENT_PROFILE = {"skills": ["SQL"], "jobs": ["Data Analyst"], "locations": ["Paris"],
                   "contracts": ["Alternance"], "years_experience": 0}


def _setup(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBWATCH_SINGLE_USER", raising=False)
    engine = create_engine("sqlite:///" + str(tmp_path / "perso.db"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sl = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "db", lambda: sl())
    return TestClient(main.app), sl


def _user(c, name):
    r = c.post("/api/auth/register", json={"username": name, "password": "secret123"})
    assert r.status_code == 200, r.text
    return r.json()["id"], {"Authorization": f"Bearer {r.json()['token']}"}


def _seed(sl, uid_a, uid_b):
    d = sl()
    d.add(Job(external_id="j1", title="Data Analyst", company="Acme", location="Paris",
              contract="CDI", skills=["SQL"], description="SQL, tableaux de bord",
              explanation={"source": "linkedin", "prescore": 9.9}))
    d.add(Profile(user_id=uid_a, status="validated", data=CDI_PROFILE))
    d.add(Profile(user_id=uid_b, status="validated", data=STUDENT_PROFILE))
    d.commit()
    return d


def test_scoring_a_user_never_writes_on_the_shared_job(tmp_path, monkeypatch):
    c, sl = _setup(tmp_path, monkeypatch)
    uid_a, _ = _user(c, "alice")
    d = _seed(sl, uid_a, uid_a + 1000)
    score_all(d, CDI_PROFILE, uid_a)
    job = d.query(Job).one()
    assert job.score is None and job.explanation == {"source": "linkedin", "prescore": 9.9}
    rating = d.query(JobRating).filter_by(user_id=uid_a).one()
    assert rating.score is not None and rating.explanation["source"] == "linkedin"
    # La pré-note est celle de CE profil (heuristique), pas celle de l'import.
    assert rating.explanation["prescore"] != 9.9


def test_unscored_user_sees_a_provisional_score_on_its_own_profile(tmp_path, monkeypatch):
    c, sl = _setup(tmp_path, monkeypatch)
    uid_a, ha = _user(c, "alice")
    uid_b, hb = _user(c, "bob")
    d = _seed(sl, uid_a, uid_b)
    score_all(d, CDI_PROFILE, uid_a)  # seule Alice a lancé une notation
    ja = c.get("/api/jobs", headers=ha).json()[0]
    jb = c.get("/api/jobs", headers=hb).json()[0]
    assert ja["score"] > 3.9 and not ja["explanation"].get("provisional")
    # Bob (alternance) ne voit pas la note d'Alice : note provisoire sur son profil.
    assert jb["explanation"]["provisional"] is True and jb["score"] <= 3.9
    assert jb["explanation"]["gates"]["contrat"] is False
    assert [s["label"] for s in jb["signals"]][:1] == ["Contrat CDI"]
    assert not any(s["kind"] == "blocking" for s in ja["signals"])
    assert jb["explanation"]["source"] == "linkedin"  # le fait partagé reste visible


def test_decision_without_rating_does_not_copy_another_users_score(tmp_path, monkeypatch):
    c, sl = _setup(tmp_path, monkeypatch)
    uid_a, ha = _user(c, "alice")
    uid_b, hb = _user(c, "bob")
    d = _seed(sl, uid_a, uid_b)
    score_all(d, CDI_PROFILE, uid_a)
    job_id = d.query(Job).one().id
    assert c.post(f"/api/jobs/{job_id}/decision", headers=hb, params={"value": "retained"}).status_code == 200
    r = sl().query(JobRating).filter_by(user_id=uid_b).one()
    assert r.score is None and r.decision == "retained"
    jb = c.get("/api/jobs", headers=hb).json()[0]
    assert jb["decision"] == "retained" and jb["explanation"]["provisional"] is True


def test_jev_cache_is_per_user(tmp_path, monkeypatch):
    c, sl = _setup(tmp_path, monkeypatch)
    uid_a, _ = _user(c, "alice")
    uid_b, _ = _user(c, "bob")
    d = _seed(sl, uid_a, uid_b)
    monkeypatch.setenv("JOBWATCH_SCORER", "auto")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    calls = []
    monkeypatch.setattr(jev, "evaluate", lambda payload, client=None: calls.append(1) or {
        k: {"score": 2, "max": 3, "confidence": 0.8} for k in payload["questions"]})
    other = {**STUDENT_PROFILE, "contracts": ["CDI"], "years_experience": 1}
    score_all(d, CDI_PROFILE, uid_a)
    score_all(d, other, uid_b)
    score_all(d, CDI_PROFILE, uid_a)  # la notation de Bob n'a pas invalidé le cache d'Alice
    assert len(calls) == 2


def test_deterministic_contract_check_overrides_a_wrong_jev_verdict():
    offer = {"title": "Responsable commercial export", "company": "A", "location": "Paris",
             "contract": "CDI", "description": "CDI, export, anglais courant"}
    profile = {"jobs": ["Responsable commercial export"], "locations": ["Paris"],
               "contracts": ["Indépendant", "CDD", "CDI"], "skills": ["export"]}
    answers = {"intitule": {"score": 3, "max": 3, "confidence": 0.9},
               "contrat": {"score": 0.02, "max": 3, "confidence": 0.99}}
    s, _c, e = score_job_jev(offer, profile, answers)
    assert e["gates"]["contrat"] is True and e["mandatory"] and e["dimensions"]["contrat"] == 10.0
    assert s > 3.9
    # Contrat réellement incompatible : Jev ou pas, l'offre reste bloquée.
    s2, _c, e2 = score_job_jev({**offer, "contract": "Stage"}, profile, answers)
    assert e2["gates"]["contrat"] is False and s2 <= 3.9


def test_dossier_shows_the_owner_evaluation(tmp_path, monkeypatch):
    from app.applications import job_payload
    c, sl = _setup(tmp_path, monkeypatch)
    uid_a, _ = _user(c, "alice")
    d = _seed(sl, uid_a, uid_a + 1000)
    job = d.query(Job).one()
    view = (7.5, 0.8, "retained", {"scorer": "heuristic"})
    payload = job_payload(job, view)
    assert payload["score"] == 7.5 and payload["decision"] == "retained"
    assert job_payload(job)["score"] is None  # sans vue : champs de l'annonce
