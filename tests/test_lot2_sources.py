"""Lot 2 : WTTJ (sitemap enrichi), La bonne alternance, pages carrières."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import careers, lba, websearch
from app.freshness import parse_date
from app.scrapers import sources_for

TODAY = datetime.now(timezone.utc)


def _iso(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# Dates et correspondance métier
# ---------------------------------------------------------------------------

def test_parse_date_rss_and_epoch_formats():
    assert parse_date("Tue, 24 Nov 2026 10:33:49 +0100").isoformat().startswith("2026-11-24T09:33:49")
    assert parse_date("1792767640768").year == 2026  # millisecondes (Lever)
    assert parse_date(1792767640).year == 2026  # secondes


def test_matches_metiers_uses_word_stems():
    assert websearch.matches_metiers("Développeuse web junior", ["Développeur web"])
    assert websearch.matches_metiers("Data Analyste (H/F)", ["Data analyst"])
    assert not websearch.matches_metiers("Développeur mobile", ["Développeur web"])
    assert websearch.matches_metiers("Dev C# .NET", ["C#"])
    # Les sigles de deux lettres comptent : « Sales Engineer » n'est pas « AI Engineer ».
    assert not websearch.matches_metiers("Pre-Sales / Sales Engineer", ["AI Engineer"])
    assert websearch.matches_metiers("Founding engineer (AI Health Companion)", ["AI Engineer"])


def test_rank_by_place_then_date_and_drop_stale():
    items = [{"title": "a", "location": "Lyon", "published_at": _iso(1)},
             {"title": "b", "location": "Paris", "published_at": _iso(10)},
             {"title": "c", "location": "Paris", "published_at": _iso(2)},
             {"title": "d", "location": "Paris", "published_at": _iso(200)}]
    kept = websearch.drop_stale(items, {"max_age_days": 30})
    assert [i["title"] for i in kept] == ["a", "b", "c"]
    ranked = websearch.rank_by_place_and_date(kept, {"locations": ["Paris"]})
    assert [i["title"] for i in ranked] == ["c", "b", "a"]


# ---------------------------------------------------------------------------
# WTTJ
# ---------------------------------------------------------------------------

def test_wttj_sitemap_filters_by_metier_and_freshness(monkeypatch):
    base = "https://www.welcometothejungle.com/fr/companies"
    index = ("<sitemapindex><sitemap><loc>https://www.welcometothejungle.com/sitemaps/"
             "job-listings.0.xml.gz</loc></sitemap></sitemapindex>")
    shard = "<urlset>" + "".join(
        f"<url><loc>{base}/{c}/jobs/{slug}</loc><lastmod>{_iso(d)}</lastmod></url>"
        for c, slug, d in [
            ("acme", "data-analyst-h-f_paris_ab12cd", 3),
            ("globex", "data-analyst-confirme_lyon", 5),
            ("initech", "data-analyst_paris", 300),       # trop ancienne
            ("boulangerie", "boulanger-h-f_paris", 1),    # autre métier
        ]) + "</urlset>"
    monkeypatch.setattr(websearch, "fetch_page",
                        lambda url, **kw: index if "index" in url else shard)
    offers = websearch.wttj({"metiers_vises": ["Data analyst"], "locations": ["Paris"]})
    assert [o["company"] for o in offers] == ["acme", "globex"]  # Paris d'abord
    assert all(o["published_at"] for o in offers)


# ---------------------------------------------------------------------------
# La bonne alternance
# ---------------------------------------------------------------------------

LBA_PAYLOAD = {
    "jobs": [
        {"identifier": {"id": "abc", "partner_label": "offres_emploi_lba", "partner_job_id": "x"},
         "workplace": {"name": "DINUM", "brand": None, "legal_name": "DINUM",
                       "location": {"address": "20 AVENUE DE SEGUR 75007 PARIS"}},
         "apply": {"url": "https://labonnealternance.apprentissage.beta.gouv.fr/offre/abc", "phone": None},
         "contract": {"type": ["Apprentissage"], "remote": "hybrid", "start": None,
                      "duration": 12, "start_type": None, "start_is_flexible": None},
         "offer": {"title": "Développeur web en alternance", "description": "Conçoit des applications.",
                   "desired_skills": ["Travailler en équipe"],
                   "to_be_acquired_skills": ["Développer une application web"],
                   "publication": {"creation": _iso(4), "expiration": _iso(-60)},
                   "status": "Active", "target_diploma": {"european": "6", "label": "Licence"},
                   "rome_codes": ["M1805"]}},
        {"identifier": {"id": "old", "partner_label": "offres_emploi_lba", "partner_job_id": "y"},
         "workplace": {"name": "Acme"}, "apply": {"url": "https://x.fr/1"}, "contract": {},
         "offer": {"title": "Développeur web", "status": "Filled", "publication": {}}},
        {"identifier": {"id": "z", "partner_label": "France Travail", "partner_job_id": "z"},
         "workplace": {"name": "Boulangerie"}, "apply": {"url": "https://x.fr/2"}, "contract": {},
         "offer": {"title": "Boulanger", "status": "Active", "publication": {}}},
    ],
    "recruiters": [{"workplace": {"name": "Candidature spontanée"}}],
    "warnings": [],
}


def test_lba_parse_jobs_maps_fields_and_skips_inactive():
    jobs = lba.parse_jobs(LBA_PAYLOAD)
    assert [j["title"] for j in jobs] == ["Développeur web en alternance", "Boulanger"]
    first = jobs[0]
    assert first["company"] == "DINUM" and first["location"] == "Paris (75007)"
    assert first["contract"].startswith("alternance") and first["remote"] == "hybride"
    assert "Licence" in first["description"] and first["published_at"] and first["expires_at"]


def _lba_env(monkeypatch, rome_answer):
    calls = []
    monkeypatch.setattr(lba, "_get", lambda params: calls.append(params) or LBA_PAYLOAD)
    monkeypatch.setattr(websearch, "resolve_zones",
                        lambda p: ([{"label": "Paris", "ft": "75D", "remote": False},
                                    {"label": "Lyon", "ft": "69123", "remote": False}], []))

    def fake_fetch(url, **kw):
        assert url.startswith(lba.ROME_LOOKUP), url  # aucun autre appel réseau
        if isinstance(rome_answer, Exception):
            raise rome_answer
        return json.dumps(rome_answer)
    monkeypatch.setattr(websearch, "fetch_page", fake_fetch)
    return calls


def test_lba_search_needs_key_and_targets_rome_codes(monkeypatch):
    calls = _lba_env(monkeypatch, {"labelsAndRomes": [
        {"label": "Développement web, intégration", "romes": ["M1805", "M1855"]},
        {"label": "Graphisme", "romes": ["E1205"]}]})
    profile = {"metiers_vises": ["Développeur web"], "contracts": ["Alternance"]}
    monkeypatch.delenv("LBA_API_KEY", raising=False)
    assert lba.search(profile) == [] and not calls  # pas de clé : aucun appel
    monkeypatch.setenv("LBA_API_KEY", "test-key")
    offers = lba.search(profile)
    # Premier groupe de métiers proposé → codes ROME ; départements des zones.
    assert calls == [[("departements", "75"), ("departements", "69"), ("romes", "M1805,M1855")]]
    # Avec des codes ROME, l'API cible le métier : pas de filtre par intitulé.
    assert [o["title"] for o in offers] == ["Développeur web en alternance", "Boulanger"]
    assert offers[0]["external_id"] == "lba-offres_emploi_lba-abc"
    assert offers[0]["_source"] == "labonnealternance"


def test_lba_profile_romes_win_and_lookup_failure_falls_back(monkeypatch):
    calls = _lba_env(monkeypatch, websearch.FairUseError("hors ligne"))
    monkeypatch.setenv("LBA_API_KEY", "test-key")
    assert lba.romes_for({"romes": ["m1805"]}) == (["M1805"], "profil")
    offers = lba.search({"metiers_vises": ["Développeur web"], "contracts": ["Alternance"]})
    assert ("romes", "M1805") not in calls[-1]
    # Recherche ROME indisponible : repli sur le filtre par intitulé.
    assert [o["title"] for o in offers] == ["Développeur web en alternance"]


def test_lba_unescapes_and_dedupes_partner_copies():
    job = LBA_PAYLOAD["jobs"][0]
    copy = json.loads(json.dumps(job))
    copy["identifier"] = {"id": "abc2", "partner_label": "Hellowork", "partner_job_id": "h1"}
    escaped = json.loads(json.dumps(job))
    escaped["identifier"] = {"id": "e", "partner_label": "France Travail", "partner_job_id": "e"}
    escaped["offer"]["title"] = "Chef de projet Social Media &amp; Ads"
    jobs = lba.parse_jobs({"jobs": [job, copy, escaped]})
    assert [j["title"] for j in jobs] == ["Développeur web en alternance",
                                          "Chef de projet Social Media & Ads"]


def test_lba_region_zone_expands_to_departements(monkeypatch):
    # Référentiel statique : aucune requête réseau (donc aucun quota consommé).
    monkeypatch.setattr(websearch, "fetch_page", lambda *a, **k: pytest.fail("réseau"))
    assert lba._departement_of("11R") == ["75", "77", "78", "91", "92", "93", "94", "95"]
    assert lba._departement_of("97411") == ["974"]


def test_sources_are_added_from_profile(monkeypatch):
    monkeypatch.delenv("LBA_API_KEY", raising=False)
    assert "labonnealternance" not in sources_for({"contracts": ["Alternance"]})
    monkeypatch.setenv("LBA_API_KEY", "k")
    assert "labonnealternance" in sources_for({"contracts": ["Alternance"]})
    assert "labonnealternance" not in sources_for({"contracts": ["CDI"]})
    assert "career_pages" in sources_for({"career_pages": ["https://jobs.lever.co/acme"]})


# ---------------------------------------------------------------------------
# Pages carrières
# ---------------------------------------------------------------------------

def test_detect_platforms():
    assert careers.detect("https://boards.greenhouse.io/acme") == ("greenhouse", "acme")
    assert careers.detect("https://job-boards.greenhouse.io/acme/jobs/1") == ("greenhouse", "acme")
    assert careers.detect("https://jobs.lever.co/acme") == ("lever", "acme")
    assert careers.detect("https://jobs.eu.lever.co/acme") == ("lever_eu", "acme")
    assert careers.detect("https://jobs.ashbyhq.com/acme") == ("ashby", "acme")
    assert careers.detect("https://careers.smartrecruiters.com/Acme1") == ("smartrecruiters", "Acme1")
    assert careers.detect("https://acme.teamtailor.com/jobs") == ("teamtailor", "https://acme.teamtailor.com")
    assert careers.detect("https://acme.fr/carrieres")[0] == "page"


GREENHOUSE = {"jobs": [
    {"title": "Data Analyst", "company_name": "Acme", "location": {"name": "Paris, France"},
     "absolute_url": "https://acme.fr/jobs/1", "content": "&lt;p&gt;Analyse &amp; SQL&lt;/p&gt;",
     "first_published": _iso(3), "metadata": [{"name": "Employment Type", "value": "Internship"}]},
    {"title": "Office Manager", "company_name": "Acme", "location": {"name": "Paris"},
     "absolute_url": "https://acme.fr/jobs/2", "content": "", "first_published": _iso(3)},
]}
LEVER = [{"text": "Data Analyst", "hostedUrl": "https://jobs.lever.co/acme/1", "createdAt": 1792767640768,
          "categories": {"location": "Paris", "commitment": "Permanent"}, "workplaceType": "hybrid",
          "descriptionPlain": "SQL et Python"}]
ASHBY = {"jobs": [{"title": "Data Analyst - internship", "location": "Paris, France", "employmentType": "Intern",
                   "publishedAt": _iso(5), "jobUrl": "https://jobs.ashbyhq.com/acme/1", "isListed": True,
                   "isRemote": False, "workplaceType": "OnSite", "descriptionPlain": "SQL"},
                  {"title": "Hidden", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/2"}]}
SMART = {"content": [{"id": "77", "name": "Stage - Data Analyst", "releasedDate": _iso(1),
                      "location": {"city": "Annecy", "region": "ARA", "remote": False},
                      "typeOfEmployment": {"label": "Full-time"}, "experienceLevel": {"id": "internship"},
                      "company": {"name": "Acme"}}]}
TEAMTAILOR = ("<rss><channel><title>Acme</title><item><title>Data Analyst</title>"
              "<description><![CDATA[<p>SQL</p>]]></description>"
              "<pubDate>Tue, 24 Nov 2026 10:33:49 +0100</pubDate>"
              "<link>https://acme.teamtailor.com/jobs/1-data-analyst</link><remoteStatus>hybrid</remoteStatus>"
              "<tt:locations><tt:location><tt:city>Paris</tt:city></tt:location></tt:locations>"
              "</item></channel></rss>")
JSONLD_PAGE = ('<html><script type="application/ld+json">[{"@type":"JobPosting","title":"Data Analyst",'
               '"hiringOrganization":{"name":"Acme"},"datePosted":"' + _iso(2)[:10] + '",'
               '"employmentType":"FULL_TIME","url":"https://acme.fr/careers/42",'
               '"jobLocation":{"address":{"addressLocality":"Paris"}}}]</script></html>')


def test_platform_parsers():
    gh = careers.parse_greenhouse(GREENHOUSE)
    assert gh[0]["description"] == "Analyse & SQL" and gh[0]["contract"] == "Internship"
    lv = careers.parse_lever(LEVER, "Acme")
    assert lv[0]["contract"] == "Permanent" and lv[0]["remote"] == "hybride"
    assert parse_date(lv[0]["published_at"]) is not None
    ab = careers.parse_ashby(ASHBY, "Acme")
    assert [j["title"] for j in ab] == ["Data Analyst - internship"]
    sr = careers.parse_smartrecruiters(SMART, "Acme1")
    assert sr[0]["contract"] == "stage" and sr[0]["url"] == "https://jobs.smartrecruiters.com/Acme1/77"
    tt = careers.parse_teamtailor_rss(TEAMTAILOR)
    assert tt[0]["company"] == "Acme" and tt[0]["location"] == "Paris" and tt[0]["description"] == "SQL"
    page = careers.parse_jsonld_page(JSONLD_PAGE, "https://acme.fr/careers")
    assert page[0]["title"] == "Data Analyst" and page[0]["published_at"]


def test_career_pages_search_filters_and_isolates_failures(monkeypatch):
    def fake(url, **kw):
        if "greenhouse" in url:
            return json.dumps(GREENHOUSE)
        if "lever" in url:
            return json.dumps(LEVER)
        if "teamtailor" in url:
            return TEAMTAILOR
        if "acme.fr/careers" in url:
            return JSONLD_PAGE
        raise websearch.SiteBlocked("bloque.fr", "(HTTP 403) ")
    monkeypatch.setattr(websearch, "fetch_page", fake)
    profile = {"metiers_vises": ["Data analyst"], "locations": ["Paris"],
               "career_pages": ["https://boards.greenhouse.io/acme", "https://jobs.lever.co/acme",
                                "https://acme.teamtailor.com", "https://acme.fr/careers",
                                "https://bloque.fr/jobs"]}
    offers = careers.search(profile)
    assert len(offers) == 4 and all(o["title"].startswith("Data Analyst") for o in offers)
    assert {o["_source"] for o in offers} == {"career_pages"}
    with pytest.raises(websearch.FairUseError):
        careers.search({**profile, "career_pages": ["https://bloque.fr/jobs"]})


def test_profile_keeps_career_pages_and_romes():
    from app.profile_analysis import normalize
    from app.schemas import ProfileData
    data = ProfileData(jobs=["Dev"], career_pages=["https://jobs.lever.co/acme"], romes=["M1805"]).model_dump()
    out = normalize(data)
    assert out["career_pages"] == ["https://jobs.lever.co/acme"] and out["romes"] == ["M1805"]


# ---------------------------------------------------------------------------
# Localisation par département et niveau lu dans la description
# ---------------------------------------------------------------------------

def test_departements_from_free_text():
    from app.geo_fr import compatible, departements
    assert departements("Clichy (92110)") == {"92"}
    assert departements("92 - Clichy") == {"92"}
    assert departements("Paris 02 - 75") == {"75"}  # « 02 » est un arrondissement
    assert departements("Hauts-de-Seine") == {"92"}
    assert "92" in departements("Île-de-France")
    assert departements("Lyon") == {"69"} and departements("London") == set()
    assert compatible(["Paris", "Hauts-de-Seine"], "Clichy (92110)") is True
    assert compatible(["Paris"], "Lyon") is False
    assert compatible(["Remote"], "Lyon") is None


def test_location_gate_accepts_same_departement():
    from app.scoring import score_job
    p = {"jobs": ["Data analyst"], "locations": ["Hauts-de-Seine"], "contracts": ["Alternance"]}
    _s, _c, e = score_job({"title": "Alternance Data Analyst H/F", "location": "Clichy (92110)"}, p)
    assert e["gates"]["localisation"] is True
    _s, _c, e = score_job({"title": "Alternance Data Analyst", "location": "Lyon (69003)"}, p)
    assert e["gates"]["localisation"] is False


def test_level_from_description_needs_explicit_wording():
    from app.contracts import extract_experience
    assert extract_experience("Data Analyst", "Vous travaillerez avec des experts et le lead data.")["level"] is None
    assert extract_experience("Data Analyst", "Profil senior attendu.")["level"] == "senior"
    assert extract_experience("Data Analyst", "Niveau : confirmé")["level"] == "senior"
    assert extract_experience("Développeur senior", "")["level"] == "senior"
    assert extract_experience("Chef de projet digital", "")["level"] is None


def test_student_offer_is_not_blocked_by_title_level():
    from app.contracts import assess
    p = {"contracts": ["Alternance"], "years_experience": 0}
    assert assess({"title": "Alternance - Tech Lead C# React"}, p)["experience_ok"] is not False
    assert assess({"title": "Tech Lead C# React", "contract": "CDI"}, p)["experience_ok"] is False
    assert assess({"title": "Alternance développeur", "description": "3 ans d'expérience exigés"},
                  p)["experience_ok"] is False


def test_aggregator_copies_are_not_split_by_same_site_rule():
    from app.dedup import source_conflict
    assert source_conflict([("linkedin", "https://l.com/1")], [("linkedin", "https://l.com/2")])
    assert not source_conflict([("labonnealternance", "https://lba.fr/1")],
                               [("labonnealternance", "https://lba.fr/2")])
