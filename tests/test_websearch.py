"""Tests de la veille web fair-use : parseurs hors réseau + règles de politesse."""
import json

import httpx
import pytest

from app import websearch
from app.scrapers import CONNECTORS, DEFAULT_SOURCES, MATRIX_CONNECTORS, fetch
from app.websearch import (
    BudgetExceeded,
    RobotsBlocked,
    SiteBlocked,
    adzuna_search_url,
    francetravail_search_url,
    hellowork_search_url,
    indeed_search_url,
    linkedin_search_url,
    parse_adzuna_html,
    parse_apec_json,
    parse_francetravail_html,
    parse_hellowork,
    parse_indeed,
    parse_linkedin,
    parse_wttj_sitemap_url,
    wttj_keyword_filter,
)

PROFILE = {"metiers_vises": ["Data analyst"], "localisation": "Paris"}

LINKEDIN_HTML = """
<ul class="jobs-search__results-list"><li>
<div class="base-search-card">
  <a class="base-card__full-link"
     href="https://fr.linkedin.com/jobs/view/data-analyst-at-acme-123?position=1&amp;refId=x"></a>
  <div class="base-search-card__info">
    <h3 class="base-search-card__title">Data Analyst</h3>
    <h4 class="base-search-card__subtitle"><a class="hidden-nested-link">Acme</a></h4>
    <span class="job-search-card__location">Paris, Île-de-France</span>
  </div>
</div></li></ul>"""

INDEED_HTML = """
<div id="mosaic-zone">
<div class="job_seen_beacon">
  <h2 class="jobTitle"><a href="/viewjob?jk=abc123&amp;from=web"><span>Data Analyst H/F</span></a></h2>
  <div class="companyName"><a data-testid="company-name">Beta Corp</a></div>
  <div class="companyLocation"><div data-testid="text-location">75001 Paris</div></div>
</div></div>"""

INDEED_BLOCKED_HTML = "<html><body><div id=\"px-captcha\">Vérification</div></body></html>"

HELLOWORK_HTML = """
<ul><li data-id-storage-target="item" data-id-storage-item-id="72876369">
<form action="/fr-fr/searchoffers/hideoffermodalframeview" method="GET">
  <input type="hidden" name="offerId" value="72876369">
  <input type="hidden" name="title" value="Alternance - Data Analyst Junior H/F">
  <input type="hidden" name="company" value="Studi CFA">
</form>
<div class="tag-secondary-s" data-cy="localisationCard">Paris 14e - 75</div>
<div class="tag-secondary-s" data-cy="contractCard">CDI</div>
<a href="/fr-fr/emplois/72876369.html"><span>Voir l'offre</span></a>
</li></ul>"""

ADZUNA_HTML = """
<article data-aid="5792025513" class="a flex">
  <h2><a href="https://www.adzuna.fr/details/5792025513" data-js="jobLink">
      <strong>Data</strong> <strong>Analyst</strong> H/F</a></h2>
  <div class="ui-job-card-info"><div class="ui-location">93210</div></div>
  <span class="max-snippet-height">Participer à la conception d'outils RH...</span>
</article>"""

FT_HTML = """
<ul class="resultsliste"><li data-id-offre="212LQCB" class="result">
<a class="media with-fav" href="/offres/recherche/detail/212LQCB">
  <div class="media-body">
    <h2 data-intitule-offre="212LQCB" class="t4 media-heading">
      <span class="media-heading-title">Data Analyst Opérations (H/F)</span></h2>
    <p class="subtext">AXA PARTNERS - <span>92 - MALAKOFF</span></p>
    <p class="description">Création de valeur par la donnée...</p>
  </div>
  <div class="media-right"><p class="contrat">CDI<span class="type-contrat">Temps plein</span></p></div>
</a></li></ul>"""

APEC_PAYLOAD = {
    "totalCount": 784,
    "resultats": [{
        "numeroOffre": "179142680W",
        "intitule": "Data Analyst F/H",
        "nomCommercial": "DATA PRAXIS",
        "lieuTexte": "Paris 01 - 75",
        "salaireTexte": "46 - 49 k€ brut annuel",
        "texteOffre": "Nous recrutons un Technical Data Analyst...",
    }],
}


# ---------------------------------------------------------------------------
# Parseurs (hors réseau)
# ---------------------------------------------------------------------------

def test_parse_linkedin_extracts_cards_and_cleans_url():
    items = parse_linkedin(LINKEDIN_HTML)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Data Analyst"
    assert item["company"] == "Acme"
    assert item["location"].startswith("Paris")
    assert item["url"] == "https://fr.linkedin.com/jobs/view/data-analyst-at-acme-123"
    assert "?" not in item["url"]  # params de tracking retirés


def test_parse_indeed_extracts_cards():
    items = parse_indeed(INDEED_HTML)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Data Analyst H/F"
    assert item["company"] == "Beta Corp"
    assert item["location"] == "75001 Paris"
    assert item["url"] == "https://fr.indeed.com/viewjob?jk=abc123"


def test_parse_indeed_detects_antibot_page():
    with pytest.raises(SiteBlocked):
        parse_indeed(INDEED_BLOCKED_HTML)


def test_parse_hellowork_extracts_hidden_fields():
    items = parse_hellowork(HELLOWORK_HTML)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Alternance - Data Analyst Junior H/F"
    assert item["company"] == "Studi CFA"
    assert item["location"] == "Paris 14e - 75"
    assert item["contract"] == "CDI"
    assert item["url"] == "https://www.hellowork.com/fr-fr/emplois/72876369.html"


def test_parse_adzuna_html_extracts_articles():
    items = parse_adzuna_html(ADZUNA_HTML)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Data Analyst H/F"
    assert item["location"] == "93210"
    assert item["url"] == "https://www.adzuna.fr/details/5792025513"
    assert "outils RH" in item["description"]


def test_parse_francetravail_html_extracts_resultats():
    items = parse_francetravail_html(FT_HTML)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Data Analyst Opérations (H/F)"
    assert item["company"] == "AXA PARTNERS"
    assert item["location"] == "92 - MALAKOFF"
    assert item["contract"].startswith("CDI")
    assert item["url"] == "https://candidat.francetravail.fr/offres/recherche/detail/212LQCB"


def test_parse_apec_json_maps_fields_and_salary():
    items = parse_apec_json(APEC_PAYLOAD)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Data Analyst F/H"
    assert item["company"] == "DATA PRAXIS"
    assert item["location"] == "Paris 01 - 75"
    assert item["salary_min"] == 46000 and item["salary_max"] == 49000
    assert item["url"].endswith("/detail-offre/179142680W")


def test_parse_wttj_sitemap_url_extracts_title_company_city():
    item = parse_wttj_sitemap_url(
        "https://www.welcometothejungle.com/fr/companies/hermes/jobs/"
        "alternance-assistant-rh-h-f_louviers_HERMS_ZRVMkY3")
    assert item is not None
    assert item["company"] == "hermes"
    assert item["location"] == "louviers"  # segments identifiants ignorés
    assert "alternance assistant rh" in item["title"].lower()


def test_parse_wttj_sitemap_url_ignores_hors_fr():
    assert parse_wttj_sitemap_url(
        "https://www.welcometothejungle.com/en/companies/pinterest/jobs/x_y") is None
    assert parse_wttj_sitemap_url("https://example.com/fr/companies/a/jobs/b") is None


def test_wttj_keyword_filter_garde_matching_et_retire_le_bruit():
    items = [
        {"title": "Data Analyst H/F"},
        {"title": "Développeur web"},
        {"title": "Senior Data analyst confirmé"},
    ]
    kept = wttj_keyword_filter(items, ["Data analyst"])
    titles = [i["title"] for i in kept]
    assert "Développeur web" not in titles
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# URLs de recherche
# ---------------------------------------------------------------------------

def test_search_urls_contiennent_metier_et_lieu():
    assert linkedin_search_url(PROFILE) == (
        "https://www.linkedin.com/jobs/search?keywords=Data+analyst&location=Paris")
    assert indeed_search_url(PROFILE) == (
        "https://fr.indeed.com/jobs?q=Data+analyst&l=Paris")
    assert hellowork_search_url(PROFILE) == (
        "https://www.hellowork.com/fr-fr/emploi/recherche.html?k=Data+analyst&l=Paris")
    assert adzuna_search_url(PROFILE) == (
        "https://www.adzuna.fr/search?q=Data+analyst&w=Paris")
    assert francetravail_search_url(PROFILE) == (
        "https://candidat.francetravail.fr/offres/recherche?motsCles=Data+analyst"
        "&lieux=Paris")



# ---------------------------------------------------------------------------
# Infrastructure fair-use : robots.txt, cache, quota (transport simulé)
# ---------------------------------------------------------------------------

@pytest.fixture
def fair_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("JOBWATCH_WEB_DELAY_MIN", "0")
    monkeypatch.setenv("JOBWATCH_WEB_DELAY_MAX", "0")
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_TTL", "3600")
    monkeypatch.setenv("JOBWATCH_WEB_DAILY_LIMIT", "50")
    websearch._robots_cache.clear()
    websearch._TRANSPORT = None
    yield
    websearch._TRANSPORT = None
    websearch._robots_cache.clear()


def _install(robots="User-agent: *\nAllow: /", pages=None, status=200,
             content_type="text/html"):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        body = (pages or {}).get(request.url.path, "<html>ok</html>")
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, text=body,
                              headers={"content-type": content_type})

    websearch._TRANSPORT = httpx.MockTransport(handler)
    return calls


def test_fetch_page_strict_policy_respects_robots_disallow(fair_env, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_ROBOTS_POLICY", "strict")
    calls = _install(robots="User-agent: *\nDisallow: /private")
    with pytest.raises(RobotsBlocked):
        websearch.fetch_page("https://exemple.fr/private/offres")
    assert calls["n"] == 1  # seul robots.txt a été appelé, pas la page


def test_fetch_page_fair_policy_proceeds_with_warning(fair_env, caplog):
    calls = _install(robots="User-agent: *\nDisallow: /offres",
                     pages={"/offres": "<html>resultats</html>"})
    with caplog.at_level("WARNING", logger="jobwatch.websearch"):
        text = websearch.fetch_page("https://exemple.fr/offres")
    assert "resultats" in text  # page chargée malgré robots défavorable…
    assert any("robots.txt" in r.message for r in caplog.records)  # …mais tracée
    assert calls["n"] == 2


def test_fetch_page_allows_when_robots_ok(fair_env):
    calls = _install(pages={"/offres": "<html>resultats</html>"})
    text = websearch.fetch_page("https://exemple.fr/offres")
    assert "resultats" in text
    assert calls["n"] == 2  # robots.txt + page


def test_fetch_page_uses_cache_on_second_call(fair_env):
    calls = _install(pages={"/offres": "<html>resultats</html>"})
    websearch.fetch_page("https://exemple.fr/offres")
    websearch.fetch_page("https://exemple.fr/offres")
    # 1 robots + 1 page ; le second appel est servi depuis le cache disque
    assert calls["n"] == 2


def test_fetch_page_daily_budget_blocks_beyond_limit(fair_env, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_DAILY_LIMIT", "1")
    _install()
    websearch.fetch_page("https://exemple.fr/a")
    with pytest.raises(BudgetExceeded):
        websearch.fetch_page("https://exemple.fr/b")


def test_fetch_page_site_blocked_on_403(fair_env):
    _install(status=403)
    with pytest.raises(SiteBlocked):
        websearch.fetch_page("https://exemple.fr/offres")


def test_fetch_page_decompresses_gzip_sitemap(fair_env):
    import gzip
    payload = gzip.compress(b"<urlset><url><loc>https://x.fr/fr/companies/a/jobs/b</loc></url></urlset>")
    calls = _install(pages={"/sitemaps/index.xml.gz": payload})
    text = websearch.fetch_page("https://exemple.fr/sitemaps/index.xml.gz")
    assert "<loc>" in text
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Intégration bout-en-bout : scrapers.fetch sur les sources (sans réseau)
# ---------------------------------------------------------------------------

WTTJ_INDEX = ("<sitemapindex><sitemap><loc>"
              "https://www.welcometothejungle.com/sitemaps/job-listings.0.xml.gz"
              "</loc></sitemap></sitemapindex>")
WTTJ_SHARD = (
    "<urlset>"
    "<url><loc>https://www.welcometothejungle.com/fr/companies/acme/jobs/"
    "data-analyst-h-f_paris</loc></url>"
    "<url><loc>https://www.welcometothejungle.com/fr/companies/acme/jobs/"
    "boulanger-h-f_paris</loc></url>"
    "</urlset>")


def _fake_geo(url, *, method="GET", json_body=None, ttl=None, headers=None):
    """Référentiels geo.api.gouv.fr simulés : Paris = commune 75056."""
    if method == "POST":
        return json.dumps(APEC_PAYLOAD)
    if url == websearch.WTTJ_SITEMAP_INDEX:
        return WTTJ_INDEX
    if "job-listings" in url:
        return WTTJ_SHARD
    if "geo.api.gouv.fr/departements" in url:
        return json.dumps([{"nom": "Paris", "code": "75"}])
    if "geo.api.gouv.fr/regions" in url:
        return json.dumps([{"nom": "Île-de-France", "code": "11"}])
    if "geo.api.gouv.fr/communes" in url:
        return json.dumps([])
    if "linkedin.com" in url:
        return LINKEDIN_HTML
    if "indeed.com" in url:
        return INDEED_HTML
    if "hellowork.com" in url:
        return HELLOWORK_HTML
    if "adzuna.fr" in url:
        return ADZUNA_HTML
    if "francetravail.fr" in url:
        return FT_HTML
    return ""


def test_fetch_aggregates_matrix_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    for var in ("FRANCE_TRAVAIL_TOKEN", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr(websearch, "fetch_page", _fake_geo)

    offers, report = fetch(PROFILE)  # les 5 sources matrice + WTTJ (sitemap)
    assert report["total_unique"] == len(offers)
    assert set(report["sources"].keys()) == set(DEFAULT_SOURCES)
    # chaque source a trouvé exactement son offre fixture
    for name in DEFAULT_SOURCES:
        assert report["sources"][name]["error"] is None, name
        assert report["sources"][name]["count"] == 1, name
    # la matrice a préparé 1 métier × 1 zone × 5 sources = 5 requêtes
    matrix = [s for s in DEFAULT_SOURCES if s in MATRIX_CONNECTORS]
    assert report["matrix"]["requests"] == len(matrix) == 5
    assert len(report["diagnostics"]) == len(matrix)
    sources_renvoyees = {o["url"].split("/")[2] for o in offers}
    assert any("linkedin" in u for u in sources_renvoyees)
    assert all(o["title"] for o in offers)


def test_fetch_keeps_legacy_optin_sources(tmp_path, monkeypatch):
    """APEC/WTTJ restent disponibles en opt-in explicite (hors matrice)."""
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(websearch, "fetch_page", _fake_geo)

    offers, report = fetch(PROFILE, sources=["apec", "wttj"])
    assert set(report["sources"].keys()) == {"apec", "wttj"}
    assert report["sources"]["apec"]["count"] == 1
    assert report["sources"]["wttj"]["count"] == 1
    # WTTJ a filtré le métier non pertinent (boulanger)
    assert not any("boulanger" in o["title"].lower() for o in offers)


def test_resolve_zones_paris_departement(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(websearch, "fetch_page", _fake_geo)
    zones, warnings = websearch.resolve_zones(
        {"localisation": "Paris", "metiers_vises": ["Data analyst"]})
    assert zones == [{"label": "Paris", "ft": "75D", "remote": False}]
    assert warnings == []


def test_resolve_zones_lieu_inconnu_degrade(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(websearch, "fetch_page", _fake_geo)
    zones, warnings = websearch.resolve_zones({"localisation": "Atlantide"})
    assert zones[0]["ft"] is None  # FT ignorée pour cette zone (n8n)
    assert any("Atlantide" in w for w in warnings)


def test_build_search_requests_matrice_et_urls_ft(tmp_path, monkeypatch):
    from app.websearch import build_search_requests
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(websearch, "fetch_page", _fake_geo)
    reqs, warnings = build_search_requests(
        {"metiers_vises": ["Data analyst", "Dev"],
         "localisation": "Paris, France"})
    # 2 métiers × 2 zones × 5 sources = 20 requêtes dédupliquées
    assert len(reqs) == 20
    assert warnings == []
    ft = [r for r in reqs if r["source"] == "france_travail"]
    assert ft
    ft_paris = [r for r in ft if r["location"] == "Paris"]
    assert ft_paris and all("lieux=75D" in r["url"] and "rayon=10" in r["url"]
                            and "offresPartenaires=true" in r["url"] for r in ft_paris)
    # Zone France : pas de filtre lieu (paramètre vide omis, comme n8n)
    ft_fr = [r for r in ft if r["location"] == "France"]
    assert ft_fr and all("lieux=" not in r["url"] for r in ft_fr)
    li = [r for r in reqs if r["source"] == "linkedin"]
    assert all("keywords=" in r["url"] for r in li)


def test_scrape_request_classe_blocked_et_no_results(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(
        websearch, "fetch_page",
        lambda url, **kw: ("<html><head><title>Just a moment…</title></head>"
                           "<h1>Vérification</h1></html>"))
    kept, diag = websearch.scrape_request(
        {"source": "indeed", "url": "https://fr.indeed.com/jobs?q=x",
         "query": "x", "location": "Paris"})
    assert kept == [] and diag["status"] == "blocked"
    monkeypatch.setattr(
        websearch, "fetch_page",
        lambda url, **kw: ("<html><head><title>Emploi</title></head>"
                           "<h1>Aucune offre trouvée</h1></html>"))
    kept, diag = websearch.scrape_request(
        {"source": "linkedin", "url": "https://www.linkedin.com/jobs/search?x=1",
         "query": "x", "location": "Paris"})
    assert kept == [] and diag["status"] == "no_results"


def test_parse_structured_jsonld_repli_universel():
    html = ('<script type="application/ld+json">{"@type":"JobPosting",'
            '"title":"Data Analyst","url":"https://exemple.fr/o/1",'
            '"hiringOrganization":{"name":"Acme"},'
            '"jobLocation":[{"address":{"addressLocality":"Paris"}}],'
            '"description":"<p>Python et SQL</p>"}'
            "</script>")
    out = websearch.parse_structured(html)
    assert out and out[0]["title"] == "Data Analyst"
    assert out[0]["company"] == "Acme" and out[0]["location"] == "Paris"


def test_parseurs_loose_survivent_au_markup_derive():
    # LinkedIn sans les classes bs4 attendues (variante n8n)
    loose_li = ('<li><div class="base-card"><span class="sr-only">'
                "Dev Python</span>"
                '<a class="base-card__full-link" '
                'href="https://fr.linkedin.com/jobs/view/x?ref=1">x</a>'
                '<h4 class="base-search-card__subtitle">ACME</h4></div></li>')
    out = websearch.parse_linkedin(loose_li)
    assert out and out[0]["title"] == "Dev Python"
    assert out[0]["url"] == "https://fr.linkedin.com/jobs/view/x"
    # Indeed : titre + jk sans carte complète
    loose_indeed = ('<div class="job_seen_beacon">'
                    '<a title="Data Analyst" href="/viewjob?jk=abcDEF1234">x</a>'
                    "</div>")
    out = websearch.parse_indeed(loose_indeed)
    assert out and out[0]["url"].endswith("jk=abcDEF1234")
    # FT : sous-texte « ENTREPRISE - 75 - PARIS » découpé au code postal
    loose_ft = ('<li data-id-offre="1"><h2 data-intitule-offre="1">Analyste</h2>'
                '<p class="subtext">ACME - 75 - PARIS</p>'
                '<p class="contrat">CDI</p>'
                '<a href="/offres/recherche/detail/ABC">voir</a></li>')
    out = websearch.parse_francetravail_html(loose_ft)
    assert out and out[0]["company"] == "ACME"
    assert out[0]["location"] == "75 - PARIS"
    assert out[0]["contract"] == "CDI"


def test_enrich_raw_offer_contrat_remote_salaire():
    raw = websearch.enrich_raw_offer(
        {"title": "Dev CDI télétravail", "description": "45k€ brut annuel",
         "location": "Paris"})
    assert raw["contract"] == "cdi"
    assert "remote" in raw.get("remote", "")
    assert (raw["salary_min"], raw["salary_max"]) == (45000, 45000)




def test_resolve_zones_sans_reseau_quota_epuise(tmp_path, monkeypatch):
    """Départements et régions se résolvent même quand le quota est épuisé."""
    def _no_budget(*a, **k):
        raise websearch.BudgetExceeded()
    monkeypatch.setattr(websearch, "fetch_page", _no_budget)
    zones, warnings = websearch.resolve_zones({"localisation": "Ile-de-France, Paris"})
    assert [z["ft"] for z in zones] == ["11R", "75D"]
    assert warnings == []
    zones, warnings = websearch.resolve_zones({"localisation": "Clichy"})
    assert zones[0]["ft"] is None and "quota" in warnings[0]


def test_matrice_quota_epuise_resume_en_un_message(tmp_path, monkeypatch):
    def _no_budget(*a, **k):
        raise websearch.BudgetExceeded()
    monkeypatch.setattr(websearch, "fetch_page", _no_budget)
    gen = websearch.scrape_matrix_steps(
        {"metiers_vises": ["Dev", "Data"], "localisation": "Paris"},
        ["hellowork", "linkedin"])
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        _raws, detail = stop.value
    starts = [e for e in events if e["step"] == "request_start"]
    assert len(starts) == 1  # une seule tentative affichée, puis un résumé
    assert any(e.get("skipped") == 3 for e in events)
    assert detail["summary"]["sources"]["linkedin"]["rate_limited"] == 2


LINKEDIN_POSTING_HTML = """
<section class="top-card-layout"><h2 class="top-card-layout__title">Lead Dev Python (H/F)</h2>
<a class="topcard__org-name-link" href="#">Acme</a><span class="topcard__flavor topcard__flavor--bullet">Lyon</span></section>
<div class="description__text"><div class="show-more-less-html__markup">
<p>Nous recrutons un <strong>lead</strong> &amp; mentor.</p><ul><li>Python</li><li>Django</li></ul></div></div>
<ul class="description__job-criteria-list">
<li class="description__job-criteria-item"><h3>Type d’emploi</h3><span>CDD</span></li>
<li class="description__job-criteria-item"><h3>Secteurs</h3><span>Logiciels et Conseil</span></li></ul>
"""


def test_linkedin_job_id():
    assert websearch.linkedin_job_id(
        "https://fr.linkedin.com/jobs/view/lead-dev-at-acme-4471548673") == "4471548673"
    assert websearch.linkedin_job_id("https://www.linkedin.com/jobs/view/4471548673/") == "4471548673"
    assert websearch.linkedin_job_id("https://www.hellowork.com/fr-fr/emplois/1.html") == ""


def test_parse_linkedin_posting():
    got = websearch.parse_linkedin_posting(LINKEDIN_POSTING_HTML)
    assert got["title"] == "Lead Dev Python (H/F)" and got["company"] == "Acme"
    assert got["location"] == "Lyon" and got["contract"] == "CDD"
    full_time = LINKEDIN_POSTING_HTML.replace("<span>CDD</span>", "<span>Temps plein</span>")
    assert "contract" not in websearch.parse_linkedin_posting(full_time)
    assert got["sectors"] == ["Logiciels", "Conseil"]
    assert got["description"].startswith("Nous recrutons un lead & mentor.")
    assert "• Python\n• Django" in got["description"]


def test_fill_linkedin_details_completes_without_overwriting(monkeypatch):
    calls = []
    def fake(url, **k):
        calls.append(url)
        return LINKEDIN_POSTING_HTML
    monkeypatch.setattr(websearch, "fetch_page", fake)
    recs = [{"url": "https://fr.linkedin.com/jobs/view/a-at-b-111111111", "description": "",
             "contract": "CDI"},
            {"url": "https://fr.linkedin.com/jobs/view/c-222222222", "description": "déjà là"},
            {"url": "https://fr.indeed.com/viewjob?jk=abc", "description": ""}]
    assert websearch.fill_linkedin_details(recs) == 1
    assert calls == ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111111111"]
    assert "Python" in recs[0]["description"] and recs[0]["contract"] == "CDI"
    assert recs[1]["description"] == "déjà là" and recs[2]["description"] == ""


def test_fill_linkedin_details_stops_at_quota(monkeypatch):
    def no_budget(*a, **k):
        raise websearch.BudgetExceeded()
    monkeypatch.setattr(websearch, "fetch_page", no_budget)
    recs = [{"url": f"https://fr.linkedin.com/jobs/view/x-{n}00000000", "description": ""}
            for n in range(1, 4)]
    assert websearch.fill_linkedin_details(recs) == 0


def test_manual_linkedin_link_uses_guest_posting(monkeypatch):
    from app import manual
    seen = []
    def fake(url, **k):
        seen.append(url)
        return LINKEDIN_POSTING_HTML
    monkeypatch.setattr(websearch, "fetch_page", fake)
    offer, meta = manual.fetch_offer("https://fr.linkedin.com/jobs/view/lead-at-acme-4471548673")
    assert seen == ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4471548673"]
    assert offer["title"] == "Lead Dev Python (H/F)" and offer["company"] == "Acme"
    assert "Django" in offer["description"]
    assert offer["url"] == "https://fr.linkedin.com/jobs/view/lead-at-acme-4471548673"
    assert "page annonce LinkedIn" in meta["extraction"]


def test_liveness_linkedin_reads_guest_posting(monkeypatch):
    from app import freshness
    seen = []
    def fake(url, **k):
        seen.append(url)
        return LINKEDIN_POSTING_HTML, url
    monkeypatch.setattr(websearch, "fetch_page_info", fake)
    res = freshness.check_offer("https://fr.linkedin.com/jobs/view/lead-at-acme-4471548673")
    assert res["status"] == "active"
    closed = LINKEDIN_POSTING_HTML.replace("</h2>", "</h2><p>N'accepte plus de candidatures</p>")
    assert freshness.page_status(closed)[0] == "expired"
    assert seen == ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4471548673"]


def test_backfill_linkedin(tmp_path, monkeypatch):
    import httpx
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    from app.models import Job
    from app.sources import backfill_linkedin
    engine = create_engine("sqlite:///" + str(tmp_path / "b.db"))
    Base.metadata.create_all(engine)
    d = sessionmaker(bind=engine)()
    base = "https://fr.linkedin.com/jobs/view/"
    d.add_all([Job(external_id="a", title="Lead Dev", company="Acme", url=base + "a-111111111", description=""),
               Job(external_id="b", title="Dev", company="B", url=base + "b-222222222", description=""),
               Job(external_id="c", title="Dev", company="C", url=base + "c-333333333", description="déjà"),
               Job(external_id="d", title="Dev", company="D", url="https://www.hellowork.com/x.html", description="")])
    d.commit()

    def fake(url, **k):
        if url.endswith("222222222"):
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        return LINKEDIN_POSTING_HTML
    monkeypatch.setattr(websearch, "fetch_page", fake)
    res = backfill_linkedin(d)
    assert (res["todo"], res["filled"], res["expired"]) == (2, 1, 1)
    a = d.query(Job).filter_by(external_id="a").one()
    assert "Django" in a.description and "python" in a.skills and a.availability == "active"
    assert d.query(Job).filter_by(external_id="b").one().availability == "expired"
    assert backfill_linkedin(d)["todo"] == 0  # relançable : plus rien à faire


HELLOWORK_OFFER_HTML = """<html><head><title>Offre Dev | HelloWork</title>
<script type="application/ld&#x2B;json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Dev Python H/F",
 "url":"https://www.hellowork.com/fr-fr/emplois/84440686.html",
 "description":"<h2>Les missions</h2><p>Coder en Python &amp; Django.</p>",
 "hiringOrganization":{"@type":"Organization","name":"Acme"},
 "datePosted":"2026-11-14T00:05:52Z","validThrough":"2099-12-14T00:05:52Z"}
</script></head><body></body></html>"""


def test_jsonld_with_encoded_plus_is_read():
    """HelloWork encode le « + » de l'attribut : ``application/ld&#x2B;json``."""
    from app import manual
    assert len(manual.jsonld_job_postings(HELLOWORK_OFFER_HTML)) == 1
    assert len(websearch.parse_structured(HELLOWORK_OFFER_HTML)) == 1


def test_fill_details_hellowork(monkeypatch):
    calls = []
    def fake(url, **k):
        calls.append(url)
        return HELLOWORK_OFFER_HTML
    monkeypatch.setattr(websearch, "fetch_page", fake)
    url = "https://www.hellowork.com/fr-fr/emplois/84440686.html"
    recs = [{"url": url, "description": "", "company": "Acme"},
            {"url": "https://www.hellowork.com/fr-fr/emploi/recherche.html?k=x", "description": ""}]
    assert websearch.fill_details(recs) == 1
    assert calls == [url]
    assert "Django" in recs[0]["description"] and recs[0]["published_at"]
    assert recs[1]["description"] == ""


def test_backfill_skips_old_offers(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    from app.models import Job
    from app.sources import backfill_details
    engine = create_engine("sqlite:///" + str(tmp_path / "o.db"))
    Base.metadata.create_all(engine)
    d = sessionmaker(bind=engine)()
    now = datetime.now(timezone.utc)
    base = "https://fr.linkedin.com/jobs/view/"
    d.add_all([Job(external_id="new", title="Dev", company="A", url=base + "a-111111111",
                   description="", published_at=now - timedelta(days=10)),
               Job(external_id="old", title="Dev", company="B", url=base + "b-222222222",
                   description="", published_at=now - timedelta(days=60))])
    d.commit()
    calls = []
    monkeypatch.setattr(websearch, "fetch_page",
                        lambda url, **k: calls.append(url) or LINKEDIN_POSTING_HTML)
    res = backfill_details(d)
    assert (res["todo"], res["too_old"], res["max_age"], res["filled"]) == (1, 1, 45, 1)
    assert calls == ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111111111"]
