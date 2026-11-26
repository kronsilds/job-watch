"""Lot 1 : fraîcheur des annonces, qualification contrat/expérience, rapprochement inter-sites."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.main as main
from app import contracts, dedup, freshness, websearch
from app.db import Base
from app.models import Application, Job, JobRating, JobSource, Profile, User
from app.scoring import score_job
from app.sources import upsert_jobs

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
STUDENT = {"skills": ["Python", "SQL"], "jobs": ["Développeur Python"],
           "locations": ["Paris"], "contracts": ["Alternance", "Stage"],
           "years_experience": 0}


def _session(tmp_path, name="lot1.db"):
    engine = create_engine("sqlite:///" + str(tmp_path / name),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Contrat et expérience
# ---------------------------------------------------------------------------

def test_international_is_not_an_internship():
    assert contracts.qualify_contract("Commercial international (H/F)") == []
    assert websearch.detect_contract("Chef de projet international") == ""


def test_apprenticeship_wins_over_cdd_and_both_student_types_are_kept():
    assert contracts.qualify_contract("Alternance - Développeur Python", "", "CDD") == ["alternance"]
    assert contracts.qualify_contract("", "", "Contrat d'apprentissage") == ["alternance"]
    assert set(contracts.qualify_contract("Stage / Alternance Data analyst")) == {"stage", "alternance"}
    assert contracts.qualify_contract("ML Engineer", "", "INTERN") == ["stage"]


def test_description_needs_a_contract_phrase():
    # « vous encadrerez des alternants » ne fait pas d'un CDI une alternance.
    assert contracts.qualify_contract("Dev", "Vous encadrerez des alternants.", "CDI") == ["cdi"]
    assert contracts.qualify_contract("Dev", "Poste en CDI, télétravail partiel.") == ["cdi"]
    assert contracts.qualify_contract("Responsable qualité de vie au travail") == []
    assert contracts.qualify_contract("Chargé d'affaires VIE Allemagne") == ["vie"]


def test_experience_years_level_and_ages():
    xp = contracts.extract_experience("Développeur senior", "5 ans d'expérience minimum")
    assert xp["min_years"] == 5 and xp["level"] == "senior"
    assert contracts.extract_experience("Dev", "Expérience exigée de 2 An(s)")["min_years"] == 2
    assert contracts.extract_experience("Dev", "3 à 5 ans d'expérience")["min_years"] == 3
    assert contracts.extract_experience("Data", "3+ years of experience")["min_years"] == 3
    # Un âge ou l'ancienneté de l'entreprise ne sont pas une expérience exigée.
    young = contracts.extract_experience("Alternance dev", "Vous avez moins de 30 ans. Débutant accepté.")
    assert young["min_years"] is None and young["beginner_ok"] and young["level"] == "junior"
    assert contracts.extract_experience(
        "Dev", "Entreprise créée il y a 10 ans, expérience appréciée")["min_years"] is None


def test_student_profile_rejects_experienced_offers():
    senior = {"title": "Développeur Python senior", "description": "5 ans d'expérience exigés."}
    fit = contracts.assess(senior, STUDENT)
    assert fit["experience_ok"] is False and fit["reasons"]
    ok = contracts.assess({"title": "Alternance - Développeur Python"}, STUDENT)
    assert ok["contract_ok"] is True and ok["experience_ok"] is True
    cdi = contracts.assess({"title": "Développeur Python", "contract": "CDI"}, STUDENT)
    assert cdi["contract_ok"] is False


def test_score_caps_experienced_offer_for_student_profile():
    s, _c, e = score_job({"title": "Développeur Python", "description": "3 ans d'expérience en Python et SQL.",
                          "skills": ["Python", "SQL"], "location": "Paris"}, STUDENT)
    assert s <= 3.9 and e["gates"]["expérience"] is False
    assert "stage ou une alternance" in e["reason"]
    s2, _c, e2 = score_job({"title": "Alternance Développeur Python", "skills": ["Python", "SQL"],
                            "location": "Paris"}, STUDENT)
    assert s2 > 7 and e2["mandatory"] and e2["contract_types"] == ["alternance"]


def test_unknown_contract_or_experience_is_never_blocking():
    s, _c, e = score_job({"title": "Développeur Python", "skills": ["Python"], "location": "Paris"}, STUDENT)
    assert e["gates"]["contrat"] is None and e["gates"]["expérience"] is None and e["mandatory"]


def test_cdi_profile_penalises_big_experience_gap_only():
    p = {"skills": ["Python"], "jobs": ["Data engineer"], "contracts": ["CDI"], "years_experience": 2}
    far = contracts.assess({"title": "Data engineer", "description": "8 ans d'expérience"}, p)
    near = contracts.assess({"title": "Data engineer", "description": "3 ans d'expérience"}, p)
    assert far["experience_ok"] is False and far["experience_fit"] == 0.0
    assert near["experience_ok"] is True and near["experience_fit"] == 0.5


def test_student_search_queries_add_contract_keyword():
    requests, _w = websearch.build_search_requests(
        {**STUDENT, "sources": ["hellowork"]}, zones=[{"label": "Paris", "ft": "75D", "remote": False}])
    queries = {r["query"] for r in requests}
    assert queries == {"Développeur Python alternance", "Développeur Python stage"}
    plain, _w = websearch.build_search_requests(
        {"jobs": ["Data analyst"], "contracts": ["CDI"], "sources": ["hellowork"]},
        zones=[{"label": "Paris", "ft": "75D", "remote": False}])
    assert [r["query"] for r in plain] == ["Data analyst"]


# ---------------------------------------------------------------------------
# Dates et fraîcheur
# ---------------------------------------------------------------------------

def test_parse_date_handles_site_formats():
    assert freshness.parse_date("Publié il y a 26 jours", NOW).date().isoformat() == "2026-10-31"
    assert freshness.parse_date("Publiée hier", NOW).date().isoformat() == "2026-11-25"
    assert freshness.parse_date("il y a 2 semaines", NOW).date().isoformat() == "2026-11-12"
    assert freshness.parse_date("2026-11-20T10:00:00.000Z", NOW).day == 20
    assert freshness.parse_date("20/11/2026", NOW).month == 11
    assert freshness.parse_date("12 novembre 2026", NOW).day == 12
    assert freshness.parse_date("3 days ago", NOW).date().isoformat() == "2026-11-23"
    assert freshness.parse_date("sans date", NOW) is None
    # Indeed : « Employeur actif » n'est pas une date de publication.
    assert freshness.find_published("Employeur actif il y a 3 jours", NOW) is None
    assert freshness.find_published("Publié il y a 3 jours", NOW) is not None


def test_freshness_rules():
    assert not freshness.freshness({"published_at": datetime(2026, 6, 2)}, 45, NOW)["fresh"]
    assert freshness.freshness({"published_at": datetime(2026, 11, 20)}, 45, NOW)["fresh"]
    assert not freshness.freshness({"expires_at": datetime(2026, 11, 1)}, 45, NOW)["fresh"]
    stale = freshness.freshness({"last_seen_at": datetime(2026, 9, 1)}, 45, NOW)
    assert not stale["fresh"] and "plus revue" in stale["reason"]
    assert not freshness.freshness({"availability": "expired"}, 45, NOW)["fresh"]
    assert freshness.max_age_days({"max_age_days": 30}) == 30
    assert freshness.max_age_days({}) == 45


def test_page_status_detects_expired_offers():
    assert freshness.page_status("<p>Cette offre n’est plus disponible.</p>")[0] == "expired"
    assert freshness.page_status("<p>No longer accepting applications</p>")[0] == "expired"
    # HelloWork : mots intercalés entre « offre » et la mention.
    assert freshness.page_status(
        "<h2>Cette offre d'emploi chez RATP n'est plus disponible</h2>")[0] == "expired"
    assert freshness.page_status("<p>Postulez à cette offre avant qu'elle ne disparaisse.</p>")[0] == "unknown"
    past = ('<script type="application/ld+json">{"@type":"JobPosting","title":"x",'
            '"validThrough":"2026-01-01"}</script>')
    assert freshness.page_status(past, now=NOW)[0] == "expired"
    live = ('<script type="application/ld+json">{"@type":"JobPosting","title":"x"}</script>'
            '<script>var tpl = "offre expirée";</script>')
    assert freshness.page_status(live)[0] == "active"  # texte de script ignoré
    redirected = freshness.page_status("<html>Résultats</html>", "https://x.fr/emploi/recherche?k=a",
                                       "https://x.fr/emplois/123.html")
    assert redirected[0] == "expired"


def test_parsers_extract_publication_dates():
    li = ('<div class="base-search-card"><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/1?x=1"></a>'
          '<h3 class="base-search-card__title">Dev</h3><h4 class="base-search-card__subtitle">Acme</h4>'
          '<time class="job-search-card__listdate" datetime="2026-10-08">il y a 1 mois</time></div>')
    assert websearch.parse_linkedin(li)[0]["published_at"] == "2026-10-08"
    ft = ('<ul><li class="result" data-id-offre="1"><a href="/offres/recherche/detail/1">'
          '<h2 data-intitule-offre="1">Dev</h2><p class="subtext">ACME - <span>75 - Paris</span></p>'
          '<p class="date"> Publié il y a 26 jours </p><p class="contrat">CDI</p></a></li></ul>')
    assert websearch.parse_francetravail_html(ft)[0]["published_at"]
    sitemap = ("<urlset><url><loc>https://www.welcometothejungle.com/fr/companies/acme/jobs/dev_paris</loc>"
               "<lastmod>2026-11-02</lastmod></url></urlset>")
    assert websearch.parse_sitemap_entries(sitemap)[0][1] == "2026-11-02"


def test_check_offer_maps_http_404_to_expired(monkeypatch):
    import httpx

    def gone(url, **kw):
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("gone", request=req, response=httpx.Response(410, request=req))

    monkeypatch.setattr(websearch, "fetch_page_info", gone)
    assert freshness.check_offer("https://x.fr/offre/1")["status"] == "expired"

    def blocked(url, **kw):
        raise websearch.SiteBlocked("x.fr", "(HTTP 403) ")

    monkeypatch.setattr(websearch, "fetch_page_info", blocked)
    assert freshness.check_offer("https://x.fr/offre/1")["status"] == "unknown"


# ---------------------------------------------------------------------------
# Rapprochement inter-sites
# ---------------------------------------------------------------------------

def test_same_offer_rules():
    a = {"title": "Développeur Python (H/F)", "company": "Acme SAS", "location": "Paris, Île-de-France, France"}
    b = {"title": "Alternance - Développeur Python", "company": "ACME", "location": "Paris - 75",
         "contract": "Alternance"}
    assert dedup.same_offer(a, b)
    assert not dedup.same_offer({**a, "location": "Lyon"}, b)  # autre ville
    assert not dedup.same_offer({"title": "Stage Développeur Python", "company": "Acme"},
                                {"title": "Alternance Développeur Python", "company": "Acme"})
    assert not dedup.same_offer({"title": "Développeur Python junior", "company": "Acme"},
                                {"title": "Développeur Python senior", "company": "Acme"})
    assert not dedup.same_offer({"title": "Data Analyst", "company": "Acme"},
                                {"title": "Data Engineer", "company": "Acme"})
    # Entreprise inconnue des deux côtés : seules des descriptions proches rapprochent.
    long = "Nous recherchons un développeur Python pour construire nos API et nos pipelines de données. " * 4
    assert dedup.same_offer({"title": "Développeur Python", "company": "Inconnue", "description": long},
                            {"title": "Développeur Python", "company": "", "description": long})
    assert not dedup.same_offer({"title": "Développeur Python", "company": "Inconnue"},
                                {"title": "Développeur Python", "company": ""})


def test_upsert_merges_the_same_offer_seen_on_two_sites(tmp_path):
    sl = _session(tmp_path)
    d = sl()
    records = [
        {"external_id": "li-1", "_source": "linkedin", "title": "Développeur Python (H/F)",
         "company": "Acme", "location": "Paris, Île-de-France, France",
         "url": "https://www.linkedin.com/jobs/view/1", "published_at": "2026-11-20"},
        {"external_id": "hw-1", "_source": "hellowork", "title": "Développeur Python",
         "company": "ACME SAS", "location": "Paris - 75", "contract": "CDI",
         "url": "https://www.hellowork.com/fr-fr/emplois/1.html",
         "description": "Description complète de l'offre côté HelloWork."},
    ]
    stats = {}
    assert upsert_jobs(d, records, stats=stats) == 1
    assert stats == {"created": 1, "merged": 1, "updated": 0}
    job = d.query(Job).one()
    assert job.contract == "CDI" and "HelloWork" in job.description  # champs complétés
    assert job.published_at is not None and job.last_seen_at is not None
    assert {s.source for s in d.query(JobSource).all()} == {"linkedin", "hellowork"}
    # Ré-import : aucune nouvelle offre, la description riche est conservée.
    assert upsert_jobs(d, [dict(records[0], description="")]) == 0
    assert d.query(Job).count() == 1 and "HelloWork" in d.query(Job).one().description
    assert dedup.job_for_external_id(d, "hw-1").id == job.id


def test_merge_duplicates_keeps_application_and_decisions(tmp_path):
    sl = _session(tmp_path)
    d = sl()
    u = User(username="camille", password_hash="x"); d.add(u); d.flush()
    first = Job(external_id="a", title="Data analyst H/F", company="Acme France", location="Lyon",
                url="https://a.fr/1", explanation={"source": "linkedin"})
    second = Job(external_id="b", title="Data Analyst", company="ACME", location="Lyon 03 - 69",
                 url="https://b.fr/1", description="longue description", explanation={"source": "hellowork"})
    d.add_all([first, second]); d.flush()
    d.add(Application(job_id=second.id, profile_id=1, user_id=u.id))
    d.add(JobRating(user_id=u.id, job_id=first.id, decision="retained"))
    d.add(JobRating(user_id=u.id, job_id=second.id, decision="new"))
    d.commit()
    assert dedup.merge_duplicates(d) == 1
    kept = d.query(Job).one()
    assert kept.id == second.id  # celle qui porte le dossier
    assert kept.description == "longue description"
    rating = d.query(JobRating).one()
    assert rating.job_id == kept.id and rating.decision == "retained"
    assert d.query(Application).one().job_id == kept.id
    assert {s.external_id for s in d.query(JobSource).all()} == {"a", "b"}


# ---------------------------------------------------------------------------
# API et migration
# ---------------------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    sl = _session(tmp_path, "api.db")
    monkeypatch.setattr(main, "db", lambda: sl())
    d = sl()
    d.add(Profile(status="validated", data={**STUDENT, "max_age_days": 30}))
    now = datetime.now(timezone.utc)
    d.add_all([
        Job(external_id="fresh", title="Alternance Développeur Python", company="Acme",
            url="https://acme.fr/jobs/1", published_at=now - timedelta(days=3), score=8.0),
        Job(external_id="old", title="Alternance Développeur Python", company="Globex",
            url="https://globex.fr/jobs/1", published_at=now - timedelta(days=90), score=8.0),
        Job(external_id="kept", title="Alternance Data", company="Initech", decision="retained",
            published_at=now - timedelta(days=90), score=6.0),
    ])
    d.commit()
    return TestClient(main.app), sl


def test_jobs_api_hides_old_offers_unless_asked(tmp_path, monkeypatch):
    client, _sl = _client(tmp_path, monkeypatch)
    shown = {j["external_id"] if "external_id" in j else j["company"] for j in client.get("/api/jobs").json()}
    assert shown == {"Acme", "Initech"}  # la retenue reste visible malgré son âge
    every = client.get("/api/jobs?include_old=true").json()
    old = next(j for j in every if j["company"] == "Globex")
    assert old["freshness"]["fresh"] is False and "publiée il y a" in old["freshness"]["reason"]
    fresh = next(j for j in every if j["company"] == "Acme")
    assert fresh["contract_label"] == "Alternance" and fresh["published_at"]
    assert client.get("/api/overview").json()["old_count"] == 1


def test_check_endpoint_marks_offer_expired(tmp_path, monkeypatch):
    client, sl = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(websearch, "fetch_page_info",
                        lambda url, **kw: ("<h1>Cette offre a expiré</h1>", url))
    job_id = sl().query(Job).filter_by(external_id="fresh").one().id
    r = client.post(f"/api/jobs/{job_id}/check").json()
    assert r["status"] == "expired" and r["availability"] == "expired"
    assert "Acme" not in {j["company"] for j in client.get("/api/jobs").json()}


def test_migration_adds_new_job_columns(tmp_path, monkeypatch):
    import app.db as dbmod
    engine = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, external_id VARCHAR, title VARCHAR, "
                          "created_at DATETIME)"))
        conn.execute(text("INSERT INTO jobs (external_id, title) VALUES ('x', 'Dev')"))
    monkeypatch.setattr(dbmod, "engine", engine)
    dbmod._add_missing_columns()
    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert {"published_at", "expires_at", "last_seen_at", "availability"} <= cols
    with engine.begin() as conn:
        assert conn.execute(text("SELECT availability FROM jobs")).scalar() == "active"


def test_profile_keeps_max_age_days():
    from app.profile_analysis import normalize
    from app.schemas import ProfileData
    data = ProfileData(jobs=["Dev"], max_age_days=30).model_dump()
    assert normalize(data)["max_age_days"] == 30
    assert "max_age_days" not in normalize({"jobs": ["Dev"], "max_age_days": 0})
