"""Pré-notation des annonces : top-5/site enrichi (LLM), puis note finale.

Sans réseau : vérifie que la passe coûteuse (LLM light) ne touche que les
annonces les mieux pré-notées de chaque site, et que la note finale
(``score_all``) est calculée après enrichissement.
"""
import os

from app.sources import prescore_and_select, upsert_jobs
from app.db import Base
import app.models  # noqa: F401 — enregistre les tables avant create_all


class FakeLLM:
    name = "fake"
    def __init__(self):
        self.calls = 0
    def enrich_offer(self, offer):
        self.calls += 1
        return {"contract": "cdi"}


def _rec(source, i, title="Data Analyst", skills=("SQL",), loc="Lyon", contract="CDI"):
    return {"external_id": f"{source}-{i}", "_source": source, "title": title,
            "company": f"C{i}", "location": loc, "contract": contract,
            "skills": list(skills), "description": "SQL Python"}


def test_prescore_selects_top5_per_source():
    recs = [_rec("apec", i) for i in range(7)]
    # Les 2 moins bonnes : intitulé hors cible -> pré-note basse.
    recs[5]["title"] = "Boulanger"
    recs[5]["skills"] = []
    recs[6]["title"] = "Boulanger"
    recs[6]["skills"] = []
    profile = {"skills": ["SQL"], "jobs": ["Data Analyst"]}
    selected, prescored = prescore_and_select(recs, profile, top_n=5)
    assert len(prescored) == 7 and len(selected) == 5
    assert all(r["_source"] == "apec" for r in selected)
    assert all("Boulanger" not in r["title"] for r in selected)
    assert all(r["_prescore"] >= 0 for r in prescored)


def test_upsert_only_enriches_selected_top(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setenv("JOBWATCH_ENRICH_LLM", "1")
    monkeypatch.delenv("JOBWATCH_ENRICH_LLM_MAX", raising=False)
    e = create_engine("sqlite:///" + str(tmp_path / "pre.db"))
    Base.metadata.create_all(e)
    d = sessionmaker(bind=e)()
    recs = [_rec("apec", i, contract="") for i in range(7)]
    for r in recs[5:]:
        r["title"] = "Boulanger"
        r["skills"] = []
    fake = FakeLLM()
    profile = {"skills": ["SQL"], "jobs": ["Data Analyst"]}
    n = upsert_jobs(d, recs, provider=fake, profile_data=profile, top_n=5)
    assert n == 7
    # Seul le top-5 a reçu la passe LLM (les 2 "Boulanger" restent sans contrat).
    assert fake.calls == 5
    from app.models import Job
    by_title = {j.title: j for j in d.query(Job).all()}
    assert by_title["Data Analyst"].contract == "cdi"
    assert by_title["Boulanger"].contract == ""
    # Pré-note tracée dans explanation, sans clé transitoire persistée.
    assert "prescore" in (by_title["Data Analyst"].explanation or {})


def test_final_score_after_enrichment_uses_completed_fields(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Job
    from app.scoring import score_all
    e = create_engine("sqlite:///" + str(tmp_path / "fin.db"))
    Base.metadata.create_all(e)
    d = sessionmaker(bind=e)()
    fake = FakeLLM()
    recs = [_rec("apec", 0, contract="")]
    upsert_jobs(d, recs, provider=fake, profile_data={"skills": ["SQL"], "jobs": ["Data Analyst"]}, top_n=5)
    assert fake.calls == 1
    score_all(d, {"skills": ["SQL"], "jobs": ["Data Analyst"], "contracts": ["CDI"], "locations": ["Lyon"]})
    j = d.query(Job).first()
    assert j.score is not None and j.score > 0
    assert "prescore" in (j.explanation or {})


def test_top_n_env_var_respected(monkeypatch):
    monkeypatch.setenv("JOBWATCH_TOP_N_PER_SOURCE", "2")
    recs = [_rec("apec", i) for i in range(4)]
    profile = {"skills": ["SQL"], "jobs": ["Data Analyst"]}
    selected, _ = prescore_and_select(recs, profile)
    assert len(selected) == 2
