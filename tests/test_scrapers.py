from app.scrapers import (
    normalize, fetch, sources_for, metiers_for, locations_for,
    register_connector, enrich_skills_from_text,
)


def _demo(profile):
    return [normalize({"title": "X", "company": "Y", "description": "Python SQL"},
                      "demo"),
            normalize({"title": "X", "company": "Y", "description": "Python SQL"},
                      "demo")]


def test_sources_and_metiers_from_profile():
    p = {"metiers_vises": ["Data analyst", "BI analyst"], "sources": "france_travail, adzuna"}
    assert sources_for(p) == ["france_travail", "adzuna"]
    assert metiers_for(p) == ["Data analyst", "BI analyst"]
    assert sources_for({}) == ["france_travail", "hellowork", "linkedin", "adzuna", "indeed"]


def test_normalize_uses_hash_external_id():
    a = normalize({"title": "Dev", "company": "Acme"}, "adzuna")
    b = normalize({"title": "Dev", "company": "Acme"}, "adzuna")
    assert a["external_id"] and a["external_id"] == b["external_id"]
    assert a["title"] == "Dev" and a["company"] == "Acme"


def test_fetch_dedups_and_reports_by_source():
    register_connector("demo", _demo)
    off, rep = fetch({"sources": ["demo"]})
    assert len(off) == 1  # 2 bruts identiques -> 1 unique
    assert rep["total_raw"] == 2 and rep["total_unique"] == 1
    assert rep["sources"]["demo"]["count"] == 2


def test_fetch_unknown_connector_reports_error_without_crashing():
    off, rep = fetch({"sources": ["nope"]})
    assert off == [] and rep["sources"]["nope"]["error"] == "connecteur inconnu"


def test_enrich_skills_from_description():
    off = {"description": "Nous cherchons du Python et du SQL."}
    assert "python" in enrich_skills_from_text(off)["skills"]
    assert "sql" in enrich_skills_from_text(off)["skills"]


def test_enrich_skills_from_title_when_no_description():
    # La compétence est aussi cherchée dans le titre (description absente).
    off = {"title": "Développeur Python/React", "description": ""}
    out = enrich_skills_from_text(off)
    assert "python" in out["skills"] and "react" in out["skills"]


def test_enrich_infers_sectors_from_matched_categories():
    off = {"title": "Lead Data Engineer", "description": "SQL et Python sur Snowflake."}
    out = enrich_skills_from_text(off)
    assert "sql" in out["skills"] and "données & analytique" in [s.lower() for s in out["sectors"]]


def test_enrich_is_idempotent_and_keeps_existing():
    off = {"title": "Backend", "description": "Python", "skills": ["SQL"], "sectors": ["Bureautique"]}
    out = enrich_skills_from_text(dict(off))
    again = enrich_skills_from_text(dict(out))
    assert [s.lower() for s in out["skills"]].count("sql") == 1  # conservée, sans doublon
    assert sorted(out["skills"]) == sorted(again["skills"])
    assert any(s.lower() == "python" for s in out["skills"])  # "python" ajouté depuis la description
    assert any(s.lower() == "sql" for s in out["skills"])     # "SQL" existant conservé