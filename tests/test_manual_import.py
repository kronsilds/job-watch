"""Injection manuelle d'une annonce repérée soi-même (lien collé).

Vérifie que la plateforme enchaîne bien pré-notation → récupération →
notation → ajout à la liste, sans réseau (page fournie par un faux
``fetch_page``) et sans modèle distant (``LLM_PROVIDER=local``).
"""
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main, manual
from app.db import Base
from app.models import Event, Job, Profile
from app.websearch import RobotsBlocked

JOB_URL = "https://www.linkedin.com/jobs/view/123?utm_source=news&trackingId=9"

_PAYLOAD = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Data Analyst BI",
    "description": "<p>Nous cherchons un profil <b>SQL</b> et Power BI pour des dashboards.</p>",
    "hiringOrganization": {"@type": "Organization", "name": "Acme"},
    "jobLocation": {"@type": "Place", "address": {
        "@type": "PostalAddress", "addressLocality": "Lyon", "addressCountry": "FR"}},
    "baseSalary": {"@type": "MonetaryAmount", "currency": "EUR", "value": {
        "@type": "QuantitativeValue", "minValue": 45000, "maxValue": 55000,
        "unitText": "YEAR"}},
    "employmentType": "CDI",
    "jobLocationType": "TELECOMMUTE",
    "url": "https://www.linkedin.com/jobs/view/123",
}
_JSONLD_HTML = (
    "<html><head><title>Data Analyst BI | LinkedIn</title>"
    '<meta property="og:site_name" content="LinkedIn"></head><body>'
    '<script type="application/ld+json">' + json.dumps(_PAYLOAD) + "</script>"
    "</body></html>")
_OG_HTML = (
    "<html><head><meta property=\"og:title\" content=\"Chef de projet data | LinkedIn\">"
    '<meta name="description" content="CDI à Nantes, télétravail partiel.">'
    "</head><body><h1>Chef de projet data</h1></body></html>")
_EMPTY_HTML = ('<html><head><meta property="og:site_name" content="LinkedIn">'
               "</head><body><p>Connectez-vous pour voir cette offre.</p></body></html>")


def _client(tmp_path, monkeypatch, html=_JSONLD_HTML):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "manual.db"),
        connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sl = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "db", lambda: sl())
    calls = []

    def fake_fetch_page(url, **kwargs):
        calls.append(url)
        if callable(html):
            return html(url)
        return html

    monkeypatch.setattr(manual.websearch, "fetch_page", fake_fetch_page)
    return TestClient(main.app), sl, calls


def _seed_profile(sl):
    d = sl()
    d.add(Profile(status="validated", data={
        "skills": ["SQL", "Power BI"], "jobs": ["Data Analyst"],
        "locations": ["Lyon"], "contracts": ["CDI"]}))
    d.commit()


def _frames(text):
    events = []
    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


# --------------------------------------------------------------- lien collé

def test_extract_url_tolerates_pasted_text():
    assert manual.extract_url("Voir l'offre : https://acme.fr/jobs/1.") == "https://acme.fr/jobs/1"
    assert manual.extract_url("https://acme.fr/jobs/1)") == "https://acme.fr/jobs/1"
    assert manual.extract_url("www.acme.fr/jobs/1") == ""
    assert manual.extract_url("") == ""


def test_external_id_is_stable_and_ignores_tracking():
    plain = manual.external_id_for("https://acme.fr/jobs/1/")
    tracked = manual.external_id_for("https://ACME.fr/jobs/1?utm_source=x&utm_medium=y")
    assert plain == tracked and plain.startswith("url-")
    assert manual.external_id_for("https://acme.fr/jobs/2") != plain


# ------------------------------------------------------------- extraction

def test_parse_offer_page_from_jsonld():
    offer, meta = manual.parse_offer_page(_JSONLD_HTML, JOB_URL)
    assert offer["title"] == "Data Analyst BI"
    assert offer["company"] == "Acme"
    assert offer["location"] == "Lyon (FR)"
    assert offer["contract"] == "cdi" and offer["remote"] == "remote"
    assert (offer["salary_min"], offer["salary_max"]) == (45000, 55000)
    assert "SQL" in offer["description"] and "<b>" not in offer["description"]
    assert "JSON-LD JobPosting" in meta["extraction"] and not meta["warnings"]


def test_parse_offer_page_falls_back_to_open_graph_without_inventing():
    offer, meta = manual.parse_offer_page(_OG_HTML, "https://fr.linkedin.com/jobs/view/9")
    assert offer["title"] == "Chef de projet data"   # suffixe du site retiré
    assert offer["description"].startswith("CDI à Nantes")
    # Aucun employeur n'est déduit de og:site_name : le champ reste vide.
    assert offer["company"] == "" and offer["salary_min"] is None


def test_parse_offer_page_reports_unreadable_page():
    offer, meta = manual.parse_offer_page(_EMPTY_HTML, JOB_URL)
    assert not offer["title"]
    assert meta["extraction"] == [] and meta["warnings"]


def test_build_offer_enriches_skills_and_keeps_extracted_values():
    offer, meta = manual.build_offer(JOB_URL, _JSONLD_HTML,
                                     overrides={"title": "Saisi à la main",
                                                "company": "Autre"})
    assert offer["title"] == "Data Analyst BI"       # l'annonce prime
    assert offer["company"] == "Acme"                # l'annonce prime
    assert "sql" in offer["skills"] and "power bi" in offer["skills"]
    assert offer["sectors"]
    assert meta["missing"] == []


def test_build_offer_uses_user_fallback_only_for_empty_fields():
    offer, meta = manual.build_offer(
        JOB_URL, _EMPTY_HTML,
        overrides={"title": "Data analyst", "company": "Acme", "description": "CDI à Lyon."})
    assert offer["title"] == "Data analyst" and offer["company"] == "Acme"
    assert offer["contract"] == "cdi" and offer["location"] == "Lyon"
    assert any("repli fourni" in entry for entry in meta["extraction"])


# --------------------------------------------------------------- pipeline

def test_import_url_adds_scored_job_to_the_list(tmp_path, monkeypatch):
    client, sl, calls = _client(tmp_path, monkeypatch)
    _seed_profile(sl)
    r = client.post("/api/jobs/import-url", json={"url": JOB_URL})
    assert r.status_code == 200
    body = r.json()
    report = body["report"]
    assert report["created"] is True and report["scored"] is True
    assert report["prescore"] is not None and body["job"]["score"] >= 9.0
    assert report["source"] == "linkedin.com"
    assert report["matched_skills"]                       # profil vs annonce
    assert calls == [JOB_URL]                             # un seul GET, page intacte

    d = sl()
    job = d.query(Job).one()
    assert job.external_id == manual.external_id_for(JOB_URL)
    assert job.url == "https://www.linkedin.com/jobs/view/123"  # URL de l'annonce
    assert job.skills                                        # compétences du texte
    assert (job.explanation or {})["source"] == "linkedin.com"
    assert [e.kind for e in d.query(Event).all()] == ["import"]
    listed = client.get("/api/jobs").json()
    assert [j["id"] for j in listed] == [job.id]


def test_import_url_twice_updates_instead_of_duplicating(tmp_path, monkeypatch):
    client, sl, _calls = _client(tmp_path, monkeypatch)
    _seed_profile(sl)
    first = client.post("/api/jobs/import-url", json={"url": JOB_URL}).json()
    second = client.post("/api/jobs/import-url", json={"url": JOB_URL + "&utm_content=b"}).json()
    assert first["report"]["created"] is True and second["report"]["created"] is False
    d = sl()
    assert d.query(Job).count() == 1                       # aucun doublon
    assert second["job"]["id"] == first["job"]["id"]


def test_import_url_stream_emits_pipeline_progress(tmp_path, monkeypatch):
    client, sl, _calls = _client(tmp_path, monkeypatch)
    _seed_profile(sl)
    r = client.post("/api/jobs/import-url/stream", json={"url": JOB_URL})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _frames(r.text)
    steps = [e["step"] for e in events if e["type"] == "progress"]
    assert steps[:2] == ["fetch", "extract"] and "prescore" in steps and "imported" in steps
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["report"]["score"] is not None


def test_import_url_requires_validated_profile(tmp_path, monkeypatch):
    client, _sl, calls = _client(tmp_path, monkeypatch)
    assert client.post("/api/jobs/import-url", json={"url": JOB_URL}).status_code == 400
    events = _frames(client.post("/api/jobs/import-url/stream",
                                 json={"url": JOB_URL}).text)
    assert events[0]["type"] == "error" and "Profil non validé" in events[0]["message"]
    assert calls == []                                     # aucune requête sortante


def test_import_url_rejects_unreadable_page_with_explicit_message(tmp_path, monkeypatch):
    client, sl, _calls = _client(tmp_path, monkeypatch, html=_EMPTY_HTML)
    _seed_profile(sl)
    r = client.post("/api/jobs/import-url", json={"url": JOB_URL})
    assert r.status_code == 400 and "Intitulé du poste introuvable" in r.json()["detail"]
    assert sl().query(Job).count() == 0


def test_import_url_rejects_invalid_link(tmp_path, monkeypatch):
    client, sl, calls = _client(tmp_path, monkeypatch)
    _seed_profile(sl)
    r = client.post("/api/jobs/import-url", json={"url": "pas un lien"})
    assert r.status_code == 400 and "Collez le lien" in r.json()["detail"]
    assert calls == []


def test_import_url_surfaces_robots_refusal(tmp_path, monkeypatch):
    def blocked(url, **kwargs):
        raise RobotsBlocked(url)

    client, sl, calls = _client(tmp_path, monkeypatch, html=blocked)
    _seed_profile(sl)
    r = client.post("/api/jobs/import-url", json={"url": JOB_URL})
    assert r.status_code == 400 and "robots.txt" in r.json()["detail"]
    assert sl().query(Job).count() == 0