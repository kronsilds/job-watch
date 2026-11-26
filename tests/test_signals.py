"""Signaux d'alerte des cartes d'offres (app/signals.py)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.db import Base
from app.models import Job, JobSource, Profile
from app.signals import job_signals

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
FRESH = {"fresh": True, "reason": "", "age_days": 10, "basis": "publication"}


def _job(**kw):
    return SimpleNamespace(**{"availability": "active", "expires_at": None, **kw})


def _labels(signals):
    return [s["label"] for s in signals]


def test_closing_soon_and_new():
    sig = job_signals(_job(expires_at=NOW + timedelta(days=4)), {}, {**FRESH, "age_days": 2}, [], now=NOW)
    assert _labels(sig) == ["Clôture dans 4 j", "Récente · 2 j"]
    assert [s["level"] for s in sig] == ["warn", "ok"] and "30/11/2026" in sig[0]["title"]
    assert _labels(job_signals(_job(expires_at=NOW + timedelta(hours=2)), {}, FRESH, [], now=NOW)) == \
        ["Clôture aujourd'hui"]
    assert job_signals(_job(expires_at=NOW + timedelta(days=20)), {}, FRESH, [], now=NOW) == []


def test_blocking_gates_are_summarised_first():
    expl = {"gates": {"intitulé": True, "contrat": False, "expérience": False, "localisation": None},
            "experience_required": {"min_years": 5.0, "level": "senior"},
            "blocking_reasons": ["contrat CDI hors des contrats recherchés (Alternance)",
                                 "expérience exigée (5 an(s) minimum · senior) incompatible"]}
    sig = job_signals(_job(), expl, {**FRESH, "age_days": 1}, [], contract_label="CDI", now=NOW)
    assert _labels(sig) == ["Contrat CDI", "5 an(s) d'exp. exigés", "Récente · 1 j"]
    assert sig[0]["level"] == "bad" and "hors des contrats" in sig[0]["title"]


def test_ended_old_and_still_online():
    ended = job_signals(_job(availability="expired"),
                        {}, {"fresh": False, "reason": "HTTP 404", "age_days": 5, "basis": "verification"}, [],
                        now=NOW)
    assert _labels(ended) == ["Terminée"] and ended[0]["title"] == "HTTP 404"
    old = job_signals(_job(), {}, {"fresh": False, "reason": "publiée il y a 90 jours", "age_days": 90,
                                   "basis": "publication"}, [], now=NOW)
    assert _labels(old) == ["Ancienne · 90 j"]
    online = job_signals(_job(), {}, {"fresh": True, "age_days": 195, "basis": "en_ligne",
                                      "note": "publiée il y a 195 jours, toujours en ligne"}, [], now=NOW)
    assert _labels(online) == ["Toujours en ligne · 195 j"]
    recent_online = job_signals(_job(), {}, {"fresh": True, "age_days": 10, "basis": "en_ligne",
                                             "note": "toujours en ligne"}, [], now=NOW)
    assert recent_online == []  # rien à signaler pour une offre récente


def test_multi_site_and_cap():
    sources = [{"source": "linkedin"}, {"source": "hellowork"}, {"source": "linkedin"}]
    assert _labels(job_signals(_job(), {}, FRESH, sources, now=NOW)) == ["Vue sur 2 sites"]
    assert job_signals(_job(), {}, FRESH, [{"source": "linkedin"}, {"source": "linkedin"}], now=NOW) == []
    expl = {"gates": {"contrat": False, "expérience": False, "localisation": False, "intitulé": False}}
    assert len(job_signals(_job(), expl, FRESH, sources, now=NOW)) == 3


def test_jobs_api_returns_signals(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "sig.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sl = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "db", lambda: sl())
    d = sl()
    d.add(Profile(status="validated", data={"jobs": ["Data analyst"], "locations": ["Paris"]}))
    now = datetime.now(timezone.utc)
    job = Job(external_id="x", title="Data analyst", company="Acme", location="Paris",
              published_at=now - timedelta(days=1), expires_at=now + timedelta(days=3), score=8.0)
    d.add(job); d.flush()
    d.add_all([JobSource(job_id=job.id, source="linkedin", external_id="x", url="https://l/1"),
               JobSource(job_id=job.id, source="hellowork", external_id="y", url="https://h/1")])
    d.commit()
    data = TestClient(main.app).get("/api/jobs").json()
    labels = [s["label"] for s in data[0]["signals"]]
    assert labels[0].startswith("Clôture") and labels[1].startswith("Récente") and labels[2] == "Vue sur 2 sites"
