"""Generic, config-driven job offer connectors.

Design
------
* **Generique et pilote par le profil** : la liste de sources a interroger est
  lue dynamiquement depuis ``profile.data["sources"]`` (fallback:
  :data:`DEFAULT_SOURCES`). Les metiers recherches viennent de
  ``profile.data["metiers_vises"]`` ou ``jobs``/``adjacent_jobs``.
* **5 sources HTML par defaut** (matrice metier x zone x source, comme le
  workflow n8n ``job-agent``) : France Travail et Adzuna utilisent leur API
  officielle quand les jetons sont configures, et leur **page de recherche
  classique** en repli ; LinkedIn, Indeed et HelloWork sont interroges via
  leur page de recherche. APEC et WTTJ restent disponibles en opt-in
  (``sources: ["apec", "wttj"]``) car plus fragiles.
* **Fair use** (module :mod:`app.websearch`) : User-Agent honnete, delai
  aleatoire, quota quotidien, cache disque, respect de robots.txt, aucun
  contournement des protections anti-bot. Lieux resolus via geo.api.gouv.fr,
  URLs enrichies (rayon FT, partenaires, filtre teletravail LinkedIn),
  parseurs tolerants + repli JSON-LD, diagnostic par requete.
* **Normalisation + de-duplication** : chaque source renvoie des enregistrements
  bruts, passes dans :func:`normalize` puis de-dupliques par ``external_id``
  (hash titre|entreprise|source).
"""

from __future__ import annotations

import hashlib
import os
import re

import httpx

from .prompts import load_keywords
from . import websearch

BASE_FRANCE_TRAVAIL = os.getenv(
    "FRANCE_TRAVAIL_URL",
    "https://api.francetravail.io/partenaire/offresdemploi/v2",
)
ADZUNA = "https://api.adzuna.com/v1/api/jobs/fr/search"

DEFAULT_SOURCES = ["france_travail", "hellowork", "linkedin", "adzuna", "indeed"]

# Sources HTML servies par la matrice métier × zone (n8n) — les autres
# (APEC/WTTJ) gardent leur connecteur historique en opt-in.
MATRIX_CONNECTORS = {"france_travail", "hellowork", "linkedin", "adzuna", "indeed"}


def _env(name, default=None):
    return os.getenv(name, default)


def metiers_for(profile: dict) -> list[str]:
    """Metiers recherches quel que soit le schéma du profil (job-watch ou job-agent)."""
    for key in ("metiers_vises", "jobs", "metiers"):
        val = profile.get(key) or []
        if isinstance(val, str):
            val = [x.strip() for x in val.split(",")]
        if val:
            return [str(x).strip() for x in val if str(x).strip()]
    return [str(x).strip() for x in (profile.get("adjacent_jobs") or []) if str(x).strip()]


def locations_for(profile: dict) -> list[str]:
    val = profile.get("localisation") or profile.get("locations") or []
    if isinstance(val, str):
        val = [x.strip() for x in val.split(",")]
    return [str(x).strip() for x in val if str(x).strip()]


def sources_for(profile: dict) -> list[str]:
    """Liste dynamique des connecteurs à interroger, depuis le profil."""
    val = profile.get("sources")
    if isinstance(val, str):
        val = [x.strip() for x in val.split(",")]
    val = val or DEFAULT_SOURCES
    out = []
    for name in val:
        name = str(name).strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def normalize(item: dict, source: str) -> dict:
    """Normalise une offre brute en schéma Job (external_id, title, ...)."""
    title = (item.get("title") or item.get("intitule") or
             item.get("job_title") or "Sans titre")
    company = item.get("company") or item.get("entreprise") or "Inconnue"
    if isinstance(company, dict):
        company = company.get("display_name") or company.get("nom") or "Inconnue"
    location = item.get("location") or item.get("localisation") or ""
    if isinstance(location, dict):
        location = location.get("display_name") or location.get("libelle") or ""
    external_id = hashlib.sha256(f"{source}|{title}|{company}".lower().encode()).hexdigest()[:12]
    contracts = item.get("contract") or item.get("contractLibelle") or item.get("typeContrat") or ""
    remote = str(item.get("remote") or item.get("dureeTravailLibelle") or "")
    skills = item.get("skills") or []
    if isinstance(skills, str):
        skills = [x.strip() for x in skills.split(",") if x.strip()]
    sectors = item.get("sectors") or item.get("secteurActivite") or []
    if isinstance(sectors, str):
        sectors = [sectors]
    salary_min = item.get("salary_min") or item.get("salaire_min")
    salary_max = item.get("salary_max") or item.get("salaire_max")
    return {
        "external_id": external_id,
        "_source": source,
        "title": str(title).strip(),
        "company": str(company).strip(),
        "location": str(location).strip(),
        "remote": str(remote).strip(),
        "contract": str(contracts).strip(),
        "salary_min": salary_min if isinstance(salary_min, int) else None,
        "salary_max": salary_max if isinstance(salary_max, int) else None,
        "sectors": sectors,
        "skills": skills,
        "url": item.get("redirect_url") or item.get("url") or "",
        "description": item.get("description") or item.get("description_entreprise") or "",
    }
def france_travail(profile: dict) -> list[dict]:
    """France Travail : API officielle si FRANCE_TRAVAIL_TOKEN, sinon page de recherche."""
    token = _env("FRANCE_TRAVAIL_TOKEN")
    if not token:
        return websearch.francetravail_html(profile)
    mots = " ".join(metiers_for(profile))
    if not mots:
        raise ValueError("Aucun métier cible dans le profil pour interroger France Travail.")
    params = {"motsCles": mots, "range": "0-49"}
    locs = locations_for(profile)
    if locs:
        params["lieuTravail"] = locs[0]
    r = httpx.get(f"{BASE_FRANCE_TRAVAIL}/offre/search",
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params, timeout=20)
    r.raise_for_status()
    return [normalize(x, "france_travail") for x in r.json().get("resultats", [])]


def adzuna(profile: dict) -> list[dict]:
    """Adzuna : API publique si clés configurées, sinon page de recherche HTML."""
    app_id, key = _env("ADZUNA_APP_ID"), _env("ADZUNA_APP_KEY")
    if not app_id or not key:
        return websearch.adzuna_html(profile)
    q = " ".join(metiers_for(profile))
    if not q:
        raise ValueError("Aucun métier cible dans le profil pour interroger Adzuna.")
    params = {"app_id": app_id, "app_key": key, "what": q,
              "results_per_page": 50, "content-type": "application/json"}
    locs = locations_for(profile)
    if locs:
        params["where"] = locs[0]
    r = httpx.get(ADZUNA, params=params, timeout=20)
    r.raise_for_status()
    return [normalize(x, "adzuna") for x in r.json().get("results", [])]


CONNECTORS = {
    "france_travail": france_travail,
    "adzuna": adzuna,
    "linkedin": websearch.linkedin,
    "indeed": websearch.indeed,
    "wttj": websearch.wttj,
    "apec": websearch.apec,
    "hellowork": websearch.hellowork,
}


def register_connector(name: str, fn) -> None:
    """Enregistre un connecteur personnalisé (site spécialisé)."""
    CONNECTORS[name] = fn


def fetch_one(profile: dict, name: str) -> tuple[list[dict] | None, str | None]:
    """Interroge UN connecteur : ``(offres|None, erreur|None)``, sans jamais lever.

    Permet aux endpoints streamés d'émettre un événement *avant* l'appel
    réseau puis un autre portant le résultat, une source à la fois. Une source
    en échec ne bloque jamais les autres.
    """
    fn = CONNECTORS.get(str(name).strip().lower())
    if fn is None:
        return None, "connecteur inconnu"
    try:
        return fn(profile) or [], None
    except Exception as exc:  # une source en échec ne bloque pas les autres
        return None, str(exc)


def fetch_iter(profile: dict, sources: list[str] | None = None):
    """Itère sur les sources et yield ``(name, offres|None, erreur|None)``.

    Permet à l'appelant (notamment les endpoints streamés) d'émettre un
    feedback de progression par connecteur. Une source en échec ne bloque
    jamais les autres.
    """
    wanted = sources_for(profile) if sources is None else [str(x).strip().lower() for x in sources]
    for name in wanted:
        got, err = fetch_one(profile, name)
        yield name, got, err


def fetch(profile: dict, sources: list[str] | None = None) -> tuple[list[dict], dict]:
    """Interroge la liste (dynamique) de sources et renvoie (offres, rapport).

    Les 5 sources HTML passent par la matrice métier × zone × source
    (:func:`websearch.scrape_matrix`, alignée sur n8n) : un seul appel agrégé
    avec diagnostics par requête. APEC/WTTJ (opt-in) gardent leur connecteur
    historique via :func:`fetch_iter`. Le rapport détaille, par source, le
    nombre d'offres récupérées, les erreurs éventuelles et — pour la matrice —
    ``requests``/``warnings``/``diagnostics`` (utile au feedback du dashboard).
    Une source en échec ne bloque jamais les autres.
    """
    wanted = sources_for(profile) if sources is None else [
        str(x).strip().lower() for x in sources]
    matrix_sources = [s for s in wanted if s in MATRIX_CONNECTORS]
    legacy_sources = [s for s in wanted if s not in MATRIX_CONNECTORS]
    items: list[dict] = []
    per_source: dict[str, dict] = {}
    matrix_detail: dict = {}
    if matrix_sources:
        try:
            raws, detail = websearch.scrape_matrix(profile, matrix_sources)
            matrix_detail = detail
            summary = detail.get("summary", {})
            grouped: dict[str, list[dict]] = {}
            for raw in raws:
                src = str(raw.get("_matrix_source") or "web")
                clean = {k: v for k, v in raw.items() if not k.startswith("_")}
                grouped.setdefault(src, []).append(normalize(clean, src))
            for src in matrix_sources:
                got = grouped.get(src, [])
                items += got
                slot = (summary.get("sources") or {}).get(src, {})
                entry: dict = {"count": len(got), "error": None}
                if slot:
                    entry.update({k: slot.get(k) for k in
                                  ("requests", "ok", "no_results", "blocked",
                                   "rate_limited", "network_error",
                                   "empty_response", "parse_error", "offers",
                                   "capped") if slot.get(k) is not None})
                if not got and slot and slot.get("requests"):
                    bad = {k: slot.get(k, 0) for k in
                           ("blocked", "rate_limited", "network_error",
                            "empty_response", "parse_error")}
                    if any(bad.values()):
                        entry["error"] = "; ".join(
                            f"{k}: {v}" for k, v in bad.items() if v)
                per_source[src] = entry
        except Exception as exc:
            for src in matrix_sources:
                per_source[src] = {"count": 0, "error": str(exc)}
    for name, got, err in fetch_iter(
            profile, legacy_sources if matrix_sources or sources is not None else None):
        if matrix_sources and name in MATRIX_CONNECTORS:
            continue
        if err is not None:
            per_source[name] = {"count": 0, "error": err}
            continue
        items += got
        per_source[name] = {"count": len(got), "error": None}
    unique = {x["external_id"]: x for x in items if x.get("external_id")}
    report = {"sources": per_source, "total_raw": len(items),
              "total_unique": len(unique)}
    summary_all = (matrix_detail.get("summary") or {}) if matrix_detail else {}
    if summary_all:
        report["matrix"] = {"requests": summary_all.get("requests", 0),
                            "warnings": summary_all.get("warnings", []),
                            "sources": summary_all.get("sources", {})}
        report["diagnostics"] = matrix_detail.get("diagnostics", [])
    return list(unique.values()), report


def enrich_skills_from_text(offer: dict) -> dict:
    """Complète `skills` et `secteurs` d'une offre depuis son titre + sa description.

    Lexique généraliste (`app/data/keywords.json`) ; pure et idempotente : n'ajoute
    que des faits réellement présents dans le texte et ne remplace jamais les
    compétences/secteurs déjà portés par l'offre. La recherche couvre le titre ET
    la description (le titre porte souvent les compétences quand la description est
    absente ou tronquée). Les `secteurs` sont déduits des catégories du lexique dans
    lesquelles au moins un mot-clé est trouvé.
    """
    skills = list(offer.get("skills") or [])
    sectors = list(offer.get("sectors") or [])
    text = " ".join(x for x in (offer.get("title"), offer.get("description")) if x)
    low = re.sub(r"\s+", " ", text).lower()
    for category, keywords in load_keywords().get("skills", {}).items():
        matched = [
            k for k in keywords
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", low)
        ]
        for skill in matched:
            if skill not in skills:
                skills.append(skill)
        if matched and category not in sectors:
            sectors.append(category)
    if skills:
        offer["skills"] = skills
    if sectors:
        offer["sectors"] = sectors
    return offer
