"""Analyse du marché : sur les annonces **du profil**, pas sur tout le catalogue.

Le catalogue ``jobs`` est partagé entre les comptes (union de leurs veilles) :
agréger toutes les annonces mélangerait les métiers de chacun. L'analyse ne
retient donc que les annonces du profil (:func:`profile_jobs`) :

* présentées à ce compte (fraîcheur selon l'âge maximum de son profil) ;
* et relevant de ses métiers visés ou connexes : l'intitulé contient leurs
  mots (racines), ou la notation de ce compte juge l'intitulé proche
  (dimension « métier » ≥ 6/10, ex. « Analyste de données » pour « Data
  analyst »).

Sans profil (appel hors compte), l'analyse porte sur tout le catalogue. Les
faits renvoyés précisent ``scope`` (``profil`` / ``catalogue``) et
``catalog_count``.
"""

from collections import Counter

from .llm import get_provider

TITLE_FIT = 6.0  # dimension « métier » (sur 10) au-delà de laquelle l'intitulé est proche


def _metiers(profile: dict) -> list[str]:
    from .scrapers import metiers_for
    out = list(metiers_for(profile))
    for extra in profile.get("adjacent_jobs") or []:
        if str(extra).strip() and extra not in out:
            out.append(str(extra).strip())
    return out


def profile_jobs(db, profile=None, user_id=None):
    """``(annonces du profil, taille du catalogue)``."""
    from .models import Job
    jobs = db.query(Job).all()
    if not profile:
        return jobs, len(jobs)
    from .freshness import freshness, max_age_days
    from .user_jobs import ratings_map
    from .websearch import matches_metiers
    metiers = _metiers(profile)
    max_age = max_age_days(profile)
    ratings = ratings_map(db, user_id)
    out = []
    for job in jobs:
        if not freshness(job, max_age)["fresh"]:
            continue
        if matches_metiers(job.title or "", metiers):
            out.append(job)
            continue
        # Évaluation de CE compte (mono-utilisateur : portée par l'annonce).
        rating = ratings.get(job.id)
        expl = (rating.explanation if rating is not None else
                (job.explanation if user_id is None else None)) or {}
        if float((expl.get("dimensions") or {}).get("intitule") or 0) >= TITLE_FIT:
            out.append(job)
    return out, len(jobs)


def analyze(db, profile=None, user_id=None):
    """Faits du marché (agrégats) sur les annonces du profil."""
    from .contracts import LABELS, qualify_contract
    jobs, catalog = profile_jobs(db, profile, user_id)
    top = lambda xs: Counter(x for a in xs for x in a).most_common(10)
    contracts = Counter(LABELS.get(t, t) for j in jobs
                        for t in qualify_contract(j.title, j.description, j.contract))
    return {"jobs_count": len(jobs), "catalog_count": catalog,
            "scope": "profil" if profile else "catalogue",
            "skills": top([j.skills or [] for j in jobs]),
            "sectors": top([j.sectors or [] for j in jobs]),
            "locations": Counter(j.location for j in jobs).most_common(10),
            "titles": Counter(j.title for j in jobs).most_common(10),
            "contracts": contracts.most_common(8)}


def _count_label(market: dict) -> str:
    if market.get("scope") == "profil":
        return (f"{market.get('jobs_count', 0)} offres correspondant au profil "
                f"(sur {market.get('catalog_count', 0)} au catalogue).")
    return f"{market.get('jobs_count', 0)} offres indexées."


def summarize(market):
    try:
        return get_provider().summarize_market(market)
    except Exception as exc:
        return {"summary": _count_label(market), "trends": [],
                "recommendation": "Analyse LLM indisponible: " + str(exc)}


def analyze_market(profile, db, user_id=None):
    """Analyseur de marché complet : faits collectés (heuristique) + analyse LLM (hypothèses)."""
    market = analyze(db, profile or None, user_id)
    try:
        enriched = get_provider().analyze_market(profile, market)
    except Exception as exc:
        enriched = {"summary": _count_label(market), "trends": [], "keywords": [],
                    "seniorities": [], "angles": [],
                    "hypotheses": ["Analyse LLM indisponible: " + str(exc)],
                    "recommendation": "Vérifiez la configuration LLM."}
    enriched["facts"] = market
    return enriched
