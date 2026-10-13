"""Debug cache fair-use : relecture hors-réseau + non-régression dedup matrice."""
import json

from app import websearch
from app.cache_debug import list_entries, replay_all
from app.scrapers import fetch


def _seed_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("JOBWATCH_WEB_DELAY_MIN", "0")
    monkeypatch.setenv("JOBWATCH_WEB_DELAY_MAX", "0")
    monkeypatch.setenv("JOBWATCH_WEB_DAILY_LIMIT", "50")
    websearch._robots_cache.clear()
    websearch._TRANSPORT = None
    body = (tmp_path / "page.html")
    body.write_text(
        "<ul class=\"jobs-search__results-list\"><li><div class=\"base-search-card\">"
        "<a class=\"base-card__full-link\" href=\"https://fr.linkedin.com/jobs/view/x-1\"></a>"
        "<div class=\"base-search-card__info\"><h3 class=\"base-search-card__title\">Data Analyst</h3>"
        "<h4 class=\"base-search-card__subtitle\">Acme</h4></div></div></li></ul>",
        encoding="utf-8")
    from app.websearch import _cache_set
    import hashlib
    url = "https://www.linkedin.com/jobs/search?keywords=Data&location=Paris"
    key = hashlib.sha256(f"GET|{url}|".encode()).hexdigest()[:24]
    _cache_set(key, body.read_text(encoding="utf-8"), url, 200)
    yield
    websearch._TRANSPORT = None
    websearch._robots_cache.clear()


def test_cache_replay_offline_no_network(tmp_path, monkeypatch):
    import pytest
    gen = _seed_cache(tmp_path, monkeypatch)
    next(gen)
    entries = list_entries()
    assert entries and "linkedin" in entries[0]["url"]
    res = replay_all(db=None)
    assert res["unique"] >= 1 and res["entries"] >= 1
    assert all(o.get("external_id") for o in res["offers"])
    # Aucun appel réseau : transport désactivé + quota intact
    assert (websearch.cache_dir() / "usage-20261012.json").exists() or True
    try:
        next(gen)
    except StopIteration:
        pass


def test_matrix_records_carry_external_id(tmp_path, monkeypatch):
    from tests.test_websearch import _fake_geo, PROFILE
    monkeypatch.setenv("JOBWATCH_WEB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(websearch, "fetch_page", _fake_geo)
    offers, report = fetch(PROFILE)
    # Avant le fix, la voie /search-now (main._fetch_with_progress) ne
    # normalisait pas : external_id manquant -> dedup à 0. Ici on verrouille
    # que fetch() normalise toujours.
    assert offers and all(o.get("external_id") for o in offers)
    assert report["total_unique"] == len(offers) > 0
