"""Analyse du marché par profil : jamais sur tout le catalogue partagé."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base
from app.market import analyze, analyze_market
from app.models import Job, JobRating, Profile

DATA = {"metiers_vises": ["Data analyst"], "locations": ["Paris"], "contracts": ["CDI"]}
BAKER = {"metiers_vises": ["Boulanger"], "locations": ["Lyon"], "contracts": ["CDI"]}


def _db(tmp_path, monkeypatch=None):
    engine = create_engine("sqlite:///" + str(tmp_path / "market.db"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sl = sessionmaker(bind=engine, expire_on_commit=False)
    if monkeypatch is not None:
        monkeypatch.delenv("JOBWATCH_SINGLE_USER", raising=False)
        monkeypatch.setattr(main, "db", lambda: sl())
    d = sl()
    now = datetime.now(timezone.utc)
    d.add_all([
        Job(external_id="a", title="Data Analyst H/F", location="Paris", contract="CDI", skills=["SQL", "Python"]),
        Job(external_id="b", title="Boulanger (H/F)", location="Lyon", contract="CDI", skills=["Pâtisserie"]),
        Job(external_id="c", title="Data Analyst", location="Paris", skills=["Excel"],
            published_at=now - timedelta(days=200)),                       # ancienne : hors marché
        Job(external_id="d", title="Analyste de données", location="Paris", contract="CDD", skills=["SQL"]),
    ])
    d.commit()
    return d, sl


def test_market_counts_only_the_profile_jobs(tmp_path):
    d, _sl = _db(tmp_path)
    facts = analyze(d, DATA)
    assert facts["scope"] == "profil" and facts["catalog_count"] == 4
    assert [t for t, _n in facts["titles"]] == ["Data Analyst H/F"]
    assert dict(facts["skills"]) == {"SQL": 1, "Python": 1}
    assert dict(facts["contracts"]) == {"CDI": 1}
    whole = analyze(d)  # hors compte : tout le catalogue
    assert whole["scope"] == "catalogue" and whole["jobs_count"] == 4


def test_title_judged_close_by_the_users_rating_is_included(tmp_path):
    d, _sl = _db(tmp_path)
    job = d.query(Job).filter_by(external_id="d").one()
    d.add(JobRating(user_id=1, job_id=job.id, score=6.0,
                    explanation={"dimensions": {"intitule": 8.0}}))
    d.commit()
    titles = [t for t, _n in analyze(d, DATA, user_id=1)["titles"]]
    assert sorted(titles) == ["Analyste de données", "Data Analyst H/F"]
    # Pour un autre compte, sa propre notation ne le juge pas proche : exclu.
    assert [t for t, _n in analyze(d, DATA, user_id=2)["titles"]] == ["Data Analyst H/F"]


def test_llm_fallback_summary_states_the_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    d, _sl = _db(tmp_path)
    out = analyze_market(DATA, d)
    assert out["facts"]["jobs_count"] == 1
    assert "1 offres correspondant au profil (sur 4 au catalogue)" in out["summary"]


def test_each_account_gets_its_own_market(tmp_path, monkeypatch):
    d, sl = _db(tmp_path, monkeypatch)
    c = TestClient(main.app)
    heads = {}
    for name, data in (("alice", DATA), ("bob", BAKER)):
        r = c.post("/api/auth/register", json={"username": name, "password": "secret123"}).json()
        heads[name] = {"Authorization": f"Bearer {r['token']}"}
        s = sl(); s.add(Profile(user_id=r["id"], status="validated", data=data)); s.commit()
    ma = c.get("/api/market", headers=heads["alice"]).json()
    mb = c.get("/api/market", headers=heads["bob"]).json()
    assert [t for t, _n in ma["titles"]] == ["Data Analyst H/F"]
    assert [t for t, _n in mb["titles"]] == ["Boulanger (H/F)"]
    assert c.get("/api/overview", headers=heads["bob"]).json()["market"]["skills"] == [["Pâtisserie", 1]]


def test_single_letter_skill_is_not_read_in_r_and_d():
    from app.scrapers import enrich_skills_from_text
    assert "r" not in (enrich_skills_from_text({"title": "Chef de projet R&D", "description": ""}).get("skills") or [])
    assert "r" in enrich_skills_from_text({"title": "Data analyst", "description": "Python, R et SQL"})["skills"]
