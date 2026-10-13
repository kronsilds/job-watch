"""Relecture hors-réseau du cache fair-use (debug campagne).

Le cache disque (``data/webcache/*.body`` + ``*.meta``) garde les pages brutes
déjà interrogées : ce module les relit SANS réseau, SANS quota, SANS délai,
avec les mêmes parseurs que la veille (``PARSERS`` + repli JSON-LD).

Usage :
  GET  /api/debug/cache          -> inventaire (url, age, taille, statut)
  POST /api/debug/cache/replay   -> re-parse + normalisation + statut BDD
  CLI  cache-list / cache-replay [--limit N] [--source indeed] [--import-missing]
"""

from __future__ import annotations

import json
import time

from . import websearch
from .websearch import (
    MAX_OFFERS_PER_PAGE,
    PARSERS,
    cache_dir,
    detect_no_results,
    detect_page_challenge,
    enrich_raw_offer,
    normalize_offer_url,
    parse_structured,
)


def _read_entry(body_path, meta_path) -> dict | None:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = body_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return {
        "key": body_path.stem,
        "url": str(meta.get("url") or ""),
        "status": meta.get("status"),
        "ts": float(meta.get("ts") or 0),
        "age_h": round((time.time() - float(meta.get("ts") or 0)) / 3600, 1),
        "size": len(body),
    }


def list_entries(limit: int = 200, source: str | None = None) -> list[dict]:
    """Inventaire du cache, plus récent d'abord (filtre optionnel par domaine)."""
    out = []
    for body in cache_dir().glob("*.body"):
        entry = _read_entry(body, body.with_suffix(".meta"))
        if not entry:
            continue
        if source and source.lower() not in entry["url"].lower():
            continue
        out.append(entry)
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out[: max(1, limit)]


def _guess_source(url: str) -> str:
    low = url.lower()
    for name, host in {
        "france_travail": "francetravail",
        "hellowork": "hellowork",
        "linkedin": "linkedin",
        "adzuna": "adzuna",
        "indeed": "indeed",
        "apec": "apec",
        "wttj": "welcometothejungle",
    }.items():
        if host in low:
            return name
    return ""


def replay_entry(entry: dict, body_text: str) -> tuple[list[dict], dict]:
    """Re-joue UNE page cachée via le même pipeline que scrape_request.

    Retourne (brutes_normalisées, diagnostic). Aucun appel réseau.
    """
    from .scrapers import normalize  # import différé (cycle websearch/scrapers)

    url = entry["url"]
    source = _guess_source(url)
    diag = {"key": entry["key"], "source": source, "url": url,
            "status": "", "extracted": 0, "kept": 0}
    if not (body_text or "").strip():
        diag["status"] = "empty_response"
        return [], diag
    challenge = detect_page_challenge(body_text)
    try:
        raw = list((PARSERS.get(source) or parse_structured)(body_text) or [])
        if not raw:
            raw = parse_structured(body_text)
        if raw:
            diag["status"] = "ok"
        elif challenge:
            diag["status"] = "blocked"
        elif detect_no_results(body_text):
            diag["status"] = "no_results"
        else:
            diag["status"] = "parse_error"
    except Exception as exc:
        diag.update(status="parse_error", message=str(exc))
        return [], diag
    diag["extracted"] = len(raw)
    diag["capped"] = len(raw) > MAX_OFFERS_PER_PAGE
    kept = []
    for item in raw[:MAX_OFFERS_PER_PAGE]:
        if not str(item.get("title") or "").strip():
            continue
        norm_url = normalize_offer_url(item.get("url") or "", source)
        if not norm_url:
            continue
        item["url"] = norm_url
        kept.append(normalize(enrich_raw_offer(item), source or "web"))
    diag["kept"] = len(kept)
    return kept, diag


def replay_all(limit: int = 200, source: str | None = None,
               db=None) -> dict:
    """Re-joue tout le cache (filtre optionnel) et croise avec la BDD.

    ``db`` optionnel : si fourni, chaque offre rejouée porte ``in_db`` +
    ``db_title`` (comparaison par ``external_id``).
    """
    offers: list[dict] = []
    diagnostics: list[dict] = []
    for entry in list_entries(limit=limit, source=source):
        try:
            body = (cache_dir() / f"{entry['key']}.body").read_text(encoding="utf-8")
        except OSError:
            continue
        kept, diag = replay_entry(entry, body)
        diagnostics.append(diag)
        offers.extend(kept)
    unique = {o["external_id"]: o for o in offers if o.get("external_id")}
    missing: list[dict] = []
    if db is not None:
        from .models import Job
        have = {r[0] for r in db.query(Job.external_id).all()}
        for eid, off in unique.items():
            off["in_db"] = eid in have
            if eid not in have:
                missing.append(off)
    else:
        for off in unique.values():
            off["in_db"] = None
    by_source: dict[str, int] = {}
    for o in unique.values():
        by_source[o.get("_source") or "?"] = by_source.get(o.get("_source") or "?", 0) + 1
    return {
        "entries": len(diagnostics),
        "extracted": sum(d.get("extracted", 0) for d in diagnostics),
        "kept": sum(d.get("kept", 0) for d in diagnostics),
        "unique": len(unique),
        "missing_in_db": len(missing),
        "by_source": by_source,
        "missing": missing[:50],
        "offers": list(unique.values())[:200],
        "diagnostics": diagnostics,
        "websearch_module": websearch.__name__,
    }
