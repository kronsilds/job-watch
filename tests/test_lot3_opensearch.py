"""Lot 3 : recherche ouverte par moteur (Tavily / SearXNG) et quota en base."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import careers, opensearch, websearch
from app.db import Base
from app.models import WebUsage
from app.scrapers import sources_for

PROFILE = {"metiers_vises": ["Data analyst"], "locations": ["Paris (75)"], "contracts": ["CDI"]}


def _recent(days=2):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def test_provider_selection(monkeypatch):
    assert opensearch.provider() == "" and "search_engine" not in sources_for({})
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888")
    assert opensearch.provider() == "searxng"
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert opensearch.provider() == "tavily"  # auto : Tavily en priorité
    monkeypatch.setenv("JOBWATCH_SEARCH_PROVIDER", "searxng")
    assert opensearch.provider() == "searxng"
    monkeypatch.setenv("JOBWATCH_SEARCH_PROVIDER", "off")
    assert opensearch.provider() == ""
    monkeypatch.setenv("JOBWATCH_SEARCH_PROVIDER", "auto")
    assert sources_for({})[-1] == "search_engine"


def test_queries_target_platforms_then_open_web_and_are_capped(monkeypatch):
    student = {"metiers_vises": ["Data analyst"], "contracts": ["Alternance", "Stage"],
               "locations": ["Paris (75)"]}
    queries = opensearch.build_queries(student)
    # Sans guillemets : une correspondance exacte divisait les résultats par 10.
    assert queries == [
        {"query": "Data analyst alternance Paris", "scope": "ats"},
        {"query": "Data analyst alternance Paris recrutement", "scope": "open"},
        {"query": "Data analyst stage Paris", "scope": "ats"},
        {"query": "Data analyst stage Paris recrutement", "scope": "open"},
    ]
    monkeypatch.setenv("JOBWATCH_SEARCH_MAX_QUERIES", "1")
    assert len(opensearch.build_queries(student)) == 1


def test_triage_finds_company_boards_and_job_pages():
    boards, pages = opensearch.triage([
        {"url": "https://jobs.lever.co/acme/123"},
        {"url": "https://boards.greenhouse.io/acme/jobs/9"},
        {"url": "https://job-boards.greenhouse.io/acme/jobs/10"},   # même entreprise
        {"url": "https://www.linkedin.com/jobs/view/1"},            # déjà couvert
        {"url": "https://acme.fr/carrieres/offre-data-analyst-42"},
        {"url": "https://acme.fr/blog/nos-valeurs"},                # pas une offre
        {"url": "https://acme.fr/emplois/recherche?q=data"},        # page de recherche
        {"url": "https://acme.teamtailor.com/jobs/1-data"},
        {"url": "https://www.glassdoor.com.hk/Job/paris-data-SRCH_IL.0,5.htm"},  # agrégateur
        {"url": "https://www.licornesociety.com/jobs/data-analyst"},           # liste
        {"url": "https://globex.fr/emplois/12345-data-analyst"},
    ])
    assert boards == ["https://jobs.lever.co/acme", "https://boards.greenhouse.io/acme",
                      "https://acme.teamtailor.com"]
    # Pages avec identifiant d'offre d'abord ; « /jobs/<nom> » sans identifiant
    # peut être une liste comme une offre : gardée, mais lue en dernier.
    assert pages == ["https://globex.fr/emplois/12345-data-analyst",
                     "https://acme.fr/carrieres/offre-data-analyst-42",
                     "https://www.licornesociety.com/jobs/data-analyst"]


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://x")
            raise httpx.HTTPStatusError("err", request=req, response=httpx.Response(self.status_code, request=req))


def test_tavily_request_and_mapping(monkeypatch):
    sent = []
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return _Resp(200, {"results": [{"url": "https://jobs.lever.co/acme/1", "title": "Data Analyst",
                                        "content": "…", "published_date": None}]})
    monkeypatch.setattr(httpx, "post", fake_post)
    out = opensearch._tavily('"Data analyst" Paris', "ats")
    url, body, headers = sent[0]
    assert url == "https://api.tavily.com/search" and headers["Authorization"] == "Bearer tvly-secret"
    assert body["include_domains"] == opensearch.ATS_DOMAINS and body["max_results"] == 20
    # Ni date ni pays : les pages des plateformes n'ont pas de date de publication.
    assert "time_range" not in body and "country" not in body and "api_key" not in body
    assert out == [{"url": "https://jobs.lever.co/acme/1", "title": "Data Analyst", "content": "…",
                    "published_at": ""}]
    opensearch._tavily("offre d'emploi x", "open")
    assert sent[1][1]["exclude_domains"] == opensearch.COVERED_DOMAINS
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(401, {}))
    with pytest.raises(RuntimeError, match="clé"):
        opensearch._tavily("x", "open")


def test_searxng_request_and_json_disabled(monkeypatch):
    sent = []
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8888/")

    def fake_get(url, params=None, headers=None, timeout=None):
        sent.append((url, params))
        return _Resp(200, {"results": [{"url": "https://acme.fr/job/1", "title": "Data Analyst",
                                        "publishedDate": "2026-11-20T00:00:00"}]})
    monkeypatch.setattr(httpx, "get", fake_get)
    out = opensearch._searxng('"Data analyst"', "ats")
    url, params = sent[0]
    assert url == "http://localhost:8888/search" and params["format"] == "json"
    assert "site:jobs.lever.co OR" in params["q"] and out[0]["published_at"].startswith("2026-11-20")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(403, {}))
    with pytest.raises(RuntimeError, match="JSON"):
        opensearch._searxng("x", "open")


def test_engine_answers_are_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    calls = []
    monkeypatch.setattr(opensearch, "_tavily", lambda q, s: calls.append(q) or [{"url": "https://a.fr/job/1"}])
    assert opensearch.run_query("q", "ats") == opensearch.run_query("q", "ats")
    assert calls == ["q"]  # second appel servi par le cache : pas de crédit consommé


def test_search_reads_boards_and_jsonld_pages_only(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.setattr(opensearch, "run_query", lambda q, s: [
        {"url": "https://jobs.lever.co/acme/1"},
        {"url": "https://globex.fr/carrieres/offre-data-analyst"},
        {"url": "https://initech.fr/emploi/actualite-data"},
    ])
    boards = []
    monkeypatch.setattr(careers, "fetch_board", lambda url: boards.append(url) or [
        {"title": "Data Analyst", "company": "Acme", "location": "Paris", "url": "https://jobs.lever.co/acme/1",
         "published_at": _recent()},
        {"title": "Office Manager", "company": "Acme", "location": "Paris", "url": "https://jobs.lever.co/acme/2",
         "published_at": _recent()}])
    jsonld = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Data Analyst H/F",'
              '"hiringOrganization":{"name":"Globex"},"datePosted":"' + _recent() + '",'
              '"jobLocation":{"address":{"addressLocality":"Paris"}}}</script>')
    pages = {"https://globex.fr/carrieres/offre-data-analyst": jsonld,
             "https://initech.fr/emploi/actualite-data": "<html><title>Data analyst : le métier</title></html>"}
    monkeypatch.setattr(websearch, "fetch_page", lambda url, **kw: pages[url])
    offers = opensearch.search(PROFILE)
    assert boards == ["https://jobs.lever.co/acme"]
    assert sorted(o["company"] for o in offers) == ["Acme", "Globex"]  # article ignoré, métier filtré
    assert {o["_source"] for o in offers} == {"search_engine"}
    monkeypatch.setenv("JOBWATCH_SEARCH_MAX_PAGES", "1")
    assert [o["company"] for o in opensearch.search(PROFILE)] == ["Acme"]  # budget de pages


def test_search_errors_surface_only_when_everything_fails(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")

    def boom(q, s):
        raise RuntimeError("Tavily : quota atteint (HTTP 432)")
    monkeypatch.setattr(opensearch, "run_query", boom)
    with pytest.raises(websearch.FairUseError, match="quota"):
        opensearch.search(PROFILE)
    monkeypatch.delenv("TAVILY_API_KEY")
    assert opensearch.search(PROFILE) == []  # aucun moteur : aucun appel


def test_daily_quota_can_live_in_database(tmp_path, monkeypatch):
    import app.db as dbmod
    engine = create_engine("sqlite:///" + str(tmp_path / "quota.db"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setenv("JOBWATCH_WEB_QUOTA_STORE", "db")
    monkeypatch.setenv("JOBWATCH_WEB_DAILY_LIMIT", "2")
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    websearch._consume_budget()
    websearch._consume_budget()
    with pytest.raises(websearch.BudgetExceeded):
        websearch._consume_budget()
    s = sessionmaker(bind=engine)()
    assert s.query(WebUsage).one().count == 2
    assert not list((tmp_path / "cache").glob("usage-*.json"))  # rien en fichier


def test_daily_quota_is_per_account(tmp_path, monkeypatch):
    """Chaque compte a son propre quota : l'un épuisé, l'autre lance sa veille."""
    import app.db as dbmod
    engine = create_engine("sqlite:///" + str(tmp_path / "quota.db"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setenv("JOBWATCH_WEB_QUOTA_STORE", "db")
    monkeypatch.setenv("JOBWATCH_WEB_DAILY_LIMIT", "1")

    @websearch.quota_scoped
    def steps(n, uid=None):
        for _ in range(n):
            websearch._consume_budget()
            yield "ok"

    assert list(steps(1, uid=1)) == ["ok"]
    with pytest.raises(websearch.BudgetExceeded):
        list(steps(1, uid=1))
    assert list(steps(1, 2)) == ["ok"]  # uid positionnel : autre compte, autre quota
    assert websearch.QUOTA_OWNER.get() is None  # contexte restauré
    s = sessionmaker(bind=engine)()
    assert sorted(r.count for r in s.query(WebUsage).all()) == [1, 1]


def test_quota_store_defaults(monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_QUOTA_STORE", "auto")
    import app.config as cfg
    monkeypatch.setattr(cfg, "DATABASE_URL", "postgresql+psycopg://u@h/db")
    assert websearch._quota_store() == "db"
    monkeypatch.setattr(cfg, "DATABASE_URL", "sqlite:///x.db")
    assert websearch._quota_store() == "file"


def test_offers_abroad_are_dropped_unless_profile_targets_abroad():
    from app.geo_fr import is_abroad
    assert is_abroad("Dallas, TX") and is_abroad("London") and is_abroad("Remote - US")
    assert not is_abroad("Vienne (38200)") and not is_abroad("Anywhere in France")
    items = [{"title": "a", "location": "Dallas, TX"}, {"title": "b", "location": "Paris, France"},
             {"title": "c", "location": "Paris, France; London, UK"}, {"title": "d", "location": ""}]
    kept = websearch.drop_abroad(items, {"locations": ["Paris"]})
    assert [i["title"] for i in kept] == ["b", "c", "d"]
    assert len(websearch.drop_abroad(items, {"locations": ["Paris", "London"]})) == 4


# ---------------------------------------------------------------------------
# Offres anciennes mais toujours affichées par l'entreprise
# ---------------------------------------------------------------------------

def _days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


def test_freshness_keeps_old_offer_confirmed_online():
    from app.freshness import freshness
    old = {"published_at": _days_ago(195), "live_confirmed_at": _days_ago(1)}
    fr = freshness(old, 45)
    assert fr["fresh"] and fr["basis"] == "en_ligne" and "195" in fr["note"]
    assert not freshness({**old, "live_confirmed_at": _days_ago(30)}, 45)["fresh"]  # confirmation trop ancienne
    assert not freshness({**old, "availability": "expired"}, 45)["fresh"]           # terminée : masquée
    assert not freshness({**old, "expires_at": _days_ago(2)}, 45)["fresh"]          # date limite passée


def test_page_check_confirms_only_when_active():
    from types import SimpleNamespace
    from app.freshness import apply_check
    job = SimpleNamespace(availability="active", availability_note="", availability_checked_at=None,
                          live_confirmed_at=None)
    apply_check(job, {"status": "unknown", "reason": "bloqué"})
    assert job.live_confirmed_at is None and job.availability_checked_at is not None
    apply_check(job, {"status": "active", "reason": "ok"})
    assert job.live_confirmed_at is not None
    apply_check(job, {"status": "expired", "reason": "404"})
    assert job.live_confirmed_at is None and job.availability == "expired"


def test_upsert_records_live_confirmation(tmp_path):
    from app.models import Job
    from app.sources import upsert_jobs
    engine = create_engine("sqlite:///" + str(tmp_path / "live.db"))
    Base.metadata.create_all(engine)
    d = sessionmaker(bind=engine, expire_on_commit=False)()
    upsert_jobs(d, [{"external_id": "a", "_source": "career_pages", "title": "Data Analyst",
                     "company": "Acme", "published_at": _recent(195), "_live": True},
                    {"external_id": "b", "_source": "linkedin", "title": "Data Analyst",
                     "company": "Globex", "published_at": _recent(195)}])
    by_id = {j.external_id: j for j in d.query(Job).all()}
    assert by_id["a"].live_confirmed_at is not None and by_id["b"].live_confirmed_at is None


def test_career_pages_keep_old_listed_offers_but_drop_expired(monkeypatch):
    monkeypatch.setattr(careers, "fetch_board", lambda url: [
        {"title": "Data Analyst", "company": "Acme", "location": "Paris", "url": "https://x/1",
         "published_at": _recent(195)},
        {"title": "Data Analyst Junior", "company": "Acme", "location": "Paris", "url": "https://x/2",
         "published_at": _recent(5), "expires_at": _recent(1)}])
    offers = careers.search({**PROFILE, "career_pages": ["https://jobs.lever.co/acme"]})
    assert [o["url"] for o in offers] == ["https://x/1"] and offers[0]["_live"] is True


def test_open_search_keeps_old_board_offers_not_old_isolated_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.setattr(opensearch, "run_query", lambda q, s: [
        {"url": "https://jobs.lever.co/acme/1"}, {"url": "https://globex.fr/emplois/12345-data-analyst"}])
    monkeypatch.setattr(careers, "fetch_board", lambda url: [
        {"title": "Data Analyst", "company": "Acme", "location": "Paris", "url": "https://jobs.lever.co/acme/1",
         "published_at": _recent(195)}])
    old_page = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Data Analyst",'
                '"hiringOrganization":{"name":"Globex"},"datePosted":"' + _recent(300) + '"}</script>')
    monkeypatch.setattr(websearch, "fetch_page", lambda url, **kw: old_page)
    offers = opensearch.search(PROFILE)
    assert [(o["company"], o.get("_live")) for o in offers] == [("Acme", True)]


def test_lba_offers_are_marked_live(monkeypatch):
    from app import lba
    monkeypatch.setenv("LBA_API_KEY", "k")
    monkeypatch.setattr(lba, "romes_for", lambda p: (["M1805"], "profil"))
    monkeypatch.setattr(lba, "departements_for", lambda p: ["75"])
    monkeypatch.setattr(lba, "_get", lambda params: {"jobs": [{
        "identifier": {"id": "1", "partner_label": "offres_emploi_lba"},
        "workplace": {"name": "Acme"}, "apply": {"url": "https://lba/1"}, "contract": {"type": ["Apprentissage"]},
        "offer": {"title": "Développeur web", "status": "Active", "publication": {"creation": _recent(100)}}}]})
    assert lba.search({"metiers_vises": ["Développeur web"], "contracts": ["Alternance"]})[0]["_live"] is True
