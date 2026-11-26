"""Recherche ouverte via un moteur de recherche (connecteur ``search_engine``).

Les agrégateurs ne voient pas tout : beaucoup d'entreprises publient sur leur
propre page carrières ou sur une plateforme de recrutement (Lever, Greenhouse,
Ashby, SmartRecruiters, Teamtailor…). Ce connecteur interroge un moteur de
recherche avec les métiers du profil, puis :

1. **découvre les entreprises** : un résultat hébergé sur une plateforme de
   recrutement donne l'adresse du tableau d'offres de l'entreprise, lu avec
   :mod:`app.careers` (toutes ses offres du métier, pas seulement le résultat) ;
2. **lit les autres pages d'offres** trouvées, en ne gardant que celles qui
   décrivent une offre en données structurées (JSON-LD ``JobPosting``).

Moteurs (``JOBWATCH_SEARCH_PROVIDER`` = ``tavily`` | ``searxng`` | ``auto``) :

* **Tavily** (service hébergé, adapté à Vercel) : ``POST
  https://api.tavily.com/search``, ``Authorization: Bearer <TAVILY_API_KEY>``,
  1 crédit par recherche (1 000 gratuits/mois) ;
* **SearXNG** (métamoteur libre, auto-hébergé) : ``GET {SEARXNG_URL}/search?
  format=json`` (le format JSON doit être activé dans ``settings.yml``).

Garde-fous : ``JOBWATCH_SEARCH_MAX_QUERIES`` recherches (défaut 4) et
``JOBWATCH_SEARCH_MAX_PAGES`` pages lues (défaut 10) par veille ; réponses du
moteur en cache 12 h ; pages lues via :func:`app.websearch.fetch_page`
(robots.txt, quota quotidien, délai). Les sites déjà couverts par d'autres
connecteurs ou fermés aux robots sont écartés.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from urllib.parse import urlparse

import httpx

from . import careers, websearch

TAVILY_URL = os.getenv("TAVILY_URL", "https://api.tavily.com/search")

# Plateformes de recrutement : résultat → tableau d'offres de l'entreprise.
ATS_DOMAINS = ["jobs.lever.co", "jobs.eu.lever.co", "boards.greenhouse.io",
               "job-boards.greenhouse.io", "jobs.ashbyhq.com",
               "jobs.smartrecruiters.com", "careers.smartrecruiters.com",
               "teamtailor.com"]
# Déjà couverts par un connecteur dédié, agrégateurs (doublons, souvent
# fermés aux robots) ou réseaux sociaux. Liste envoyée au moteur...
COVERED_DOMAINS = ["linkedin.com", "indeed.com", "indeed.fr", "hellowork.com",
                   "francetravail.fr", "pole-emploi.fr", "adzuna.fr",
                   "welcometothejungle.com", "apec.fr", "labonnealternance.apprentissage.beta.gouv.fr",
                   "glassdoor.fr", "glassdoor.com", "jobteaser.com", "monster.fr",
                   "cadremploi.fr", "jooble.org", "talent.com", "jobijoba.com",
                   "optioncarriere.com", "meteojob.com", "directemploi.com", "stage.fr",
                   "facebook.com", "twitter.com", "x.com", "youtube.com"]
# ... et, côté résultats, reconnue par la marque quel que soit le pays
# (« glassdoor.com.hk », « fr.jooble.org »).
_COVERED_BRANDS = re.compile(
    r"(?:^|\.)(?:linkedin|indeed|hellowork|francetravail|pole-emploi|adzuna|"
    r"welcometothejungle|apec|labonnealternance|glassdoor|jobteaser|monster|cadremploi|"
    r"jooble|talent|jobijoba|optioncarriere|meteojob|directemploi|stage|facebook|twitter|"
    r"youtube|x)\.")
_JOB_PATH = re.compile(r"job|emploi|offre|career|carriere|recrut|poste|vacanc|position|annonce",
                       re.I)
_LISTING = re.compile(r"/(?:search|recherche|resultats?)\b|[?&](?:q|query|keywords?|k|page)=|"
                      r"lists?-offres?|nos-offres|-jobs/?$|SRCH_|/jobs/?$|/offres/?$", re.I)
_OFFER_ID = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name) or default))
    except ValueError:
        return default


def provider() -> str:
    """Moteur configuré : ``tavily``, ``searxng`` ou '' (aucun)."""
    wanted = (os.getenv("JOBWATCH_SEARCH_PROVIDER") or "auto").strip().lower()
    has_tavily = bool((os.getenv("TAVILY_API_KEY") or "").strip())
    has_searx = bool((os.getenv("SEARXNG_URL") or "").strip())
    if wanted == "tavily":
        return "tavily" if has_tavily else ""
    if wanted == "searxng":
        return "searxng" if has_searx else ""
    if wanted in ("off", "none", "0"):
        return ""
    return "tavily" if has_tavily else ("searxng" if has_searx else "")


def enabled() -> bool:
    return bool(provider())


# ---------------------------------------------------------------------------
# Requêtes
# ---------------------------------------------------------------------------

def build_queries(profile: dict) -> list[dict]:
    """Recherches à lancer : ``{query, scope}`` avec ``scope`` = ``ats`` | ``open``.

    Pour chaque métier (et type de contrat d'un profil stage/alternance) : une
    recherche ciblée sur les plateformes de recrutement, puis une recherche
    ouverte « … recrutement ». Plafonnées à ``JOBWATCH_SEARCH_MAX_QUERIES``.
    """
    from .contracts import fold, search_suffixes
    from .scrapers import locations_for, metiers_for
    place = next((re.sub(r"\s*\(.*?\)\s*", " ", loc).strip()
                  for loc in locations_for(profile) if loc.strip()), "")
    terms: list[str] = []
    for metier in metiers_for(profile):
        for suffix in search_suffixes(profile):
            # Sans guillemets : une correspondance exacte divise les résultats par 10.
            term = metier + (f" {suffix}" if suffix and suffix not in fold(metier) else "")
            if term not in terms:
                terms.append(term)
    out: list[dict] = []
    for term in terms:
        base = " ".join(x for x in (term, place) if x)
        out.append({"query": base, "scope": "ats"})
        out.append({"query": f"{base} recrutement", "scope": "open"})
    return out[:_env_int("JOBWATCH_SEARCH_MAX_QUERIES", 4)]


# ---------------------------------------------------------------------------
# Moteurs (réponse normalisée : [{url, title, content, published_at}])
# ---------------------------------------------------------------------------

def _cached(key: str, fetch) -> list[dict]:
    """Réponse du moteur en cache disque 12 h (économise crédits et requêtes)."""
    ckey = "search-" + hashlib.sha256(key.encode()).hexdigest()[:24]
    hit = websearch._cache_get(ckey, _env_int("JOBWATCH_SEARCH_CACHE_TTL", 43200))
    if hit is not None:
        try:
            return json.loads(hit)
        except ValueError:
            pass
    results = fetch()
    websearch._cache_set(ckey, json.dumps(results, ensure_ascii=False), key, 200)
    return results


def _tavily(query: str, scope: str) -> list[dict]:
    # Ni ``time_range`` (les pages des plateformes n'affichent pas de date de
    # publication et seraient écartées) ni ``country`` (la ville est dans la
    # requête) : la fraîcheur et l'étranger sont filtrés ensuite, offre par offre.
    body = {"query": query, "search_depth": "basic", "topic": "general",
            "max_results": 20, "include_answer": False, "include_raw_content": False}
    if scope == "ats":
        body["include_domains"] = ATS_DOMAINS
    else:
        body["exclude_domains"] = COVERED_DOMAINS
    r = httpx.post(TAVILY_URL, json=body, timeout=30, headers={
        "Authorization": f"Bearer {(os.getenv('TAVILY_API_KEY') or '').strip()}"})
    if r.status_code == 401:
        raise RuntimeError("Tavily : clé d'API refusée (TAVILY_API_KEY)")
    if r.status_code in (429, 432, 433):
        raise RuntimeError(f"Tavily : quota atteint (HTTP {r.status_code})")
    r.raise_for_status()
    return [{"url": x.get("url") or "", "title": x.get("title") or "",
             "content": x.get("content") or "", "published_at": x.get("published_date") or ""}
            for x in (r.json().get("results") or [])]


def _searxng(query: str, scope: str) -> list[dict]:
    base = (os.getenv("SEARXNG_URL") or "").rstrip("/")
    q = query
    if scope == "ats":
        q += " (" + " OR ".join(f"site:{d}" for d in ATS_DOMAINS) + ")"
    params = {"q": q, "format": "json", "language": "fr", "safesearch": 0,
              "categories": "general"}
    r = httpx.get(f"{base}/search", params=params, timeout=30,
                  headers={"Accept": "application/json",
                           "User-Agent": websearch.user_agent()})
    if r.status_code == 403:
        raise RuntimeError("SearXNG : format JSON désactivé (ajoutez json à "
                           "search.formats dans settings.yml)")
    r.raise_for_status()
    return [{"url": x.get("url") or "", "title": x.get("title") or "",
             "content": x.get("content") or "", "published_at": x.get("publishedDate") or ""}
            for x in (r.json().get("results") or [])]


def run_query(query: str, scope: str) -> list[dict]:
    name = provider()
    fn = {"tavily": _tavily, "searxng": _searxng}.get(name)
    if fn is None:
        return []
    return _cached(f"{name}|{scope}|{query}", lambda: fn(query, scope))


# ---------------------------------------------------------------------------
# Tri des résultats
# ---------------------------------------------------------------------------

def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _covered(url: str) -> bool:
    return bool(_COVERED_BRANDS.search("." + _host(url)))


def board_url(url: str) -> str:
    """Tableau d'offres de l'entreprise si le résultat est sur une plateforme connue."""
    kind, ident = careers.detect(url)
    return {
        "greenhouse": f"https://boards.greenhouse.io/{ident}",
        "lever": f"https://jobs.lever.co/{ident}",
        "lever_eu": f"https://jobs.eu.lever.co/{ident}",
        "ashby": f"https://jobs.ashbyhq.com/{ident}",
        "smartrecruiters": f"https://careers.smartrecruiters.com/{ident}",
        "teamtailor": ident,
    }.get(kind, "")


def triage(results: list[dict]) -> tuple[list[str], list[str]]:
    """``(tableaux d'entreprises, pages d'offres)`` à lire, sans doublon."""
    boards: list[str] = []
    pages: list[str] = []
    for res in results:
        url = str(res.get("url") or "").strip()
        if not url.startswith("http") or _covered(url):
            continue
        board = board_url(url)
        if board:
            if board not in boards:
                boards.append(board)
            continue
        path = urlparse(url).path + "?" + (urlparse(url).query or "")
        if _JOB_PATH.search(path) and not _LISTING.search(path) and url not in pages:
            pages.append(url)
    # Pages avec un identifiant d'offre d'abord : le budget de lecture va
    # d'abord aux annonces plutôt qu'aux pages de présentation.
    pages.sort(key=lambda u: not _OFFER_ID.search(urlparse(u).path))
    return boards, pages


# ---------------------------------------------------------------------------
# Connecteur
# ---------------------------------------------------------------------------

def search(profile: dict) -> list[dict]:
    """Connecteur ``search_engine`` : offres découvertes via le moteur configuré."""
    from .manual import parse_offer_page
    from .scrapers import metiers_for, normalize
    if not enabled():
        return []
    results: list[dict] = []
    errors: list[str] = []
    for q in build_queries(profile):
        try:
            results += run_query(q["query"], q["scope"])
        except Exception as exc:  # une recherche en échec n'arrête pas les autres
            errors.append(str(exc))
    if errors and not results:
        raise websearch.FairUseError("; ".join(dict.fromkeys(errors)))
    boards, pages = triage(results)
    budget = _env_int("JOBWATCH_SEARCH_MAX_PAGES", 10)
    # Au plus deux tiers du budget pour les tableaux d'entreprises, le reste
    # pour les pages d'offres isolées.
    board_budget = max(1, (budget * 2) // 3) if pages else budget
    items: list[dict] = []
    for board in boards[:board_budget]:
        if budget <= 0:
            break
        budget -= 1
        try:
            # Offres du tableau de l'entreprise : ouvertes, quel que soit leur âge.
            items += [{**i, "_live": True} for i in careers.fetch_board(board)
                      if i.get("title") and i.get("url")]
        except Exception:
            continue
    for url in pages:
        if budget <= 0:
            break
        budget -= 1
        try:
            offer, meta = parse_offer_page(websearch.fetch_page(url), url)
        except Exception:
            continue
        # Seules les pages qui décrivent une offre (JobPosting) sont gardées :
        # un article ou une page d'accueil n'est pas une annonce.
        if "JSON-LD JobPosting" in (meta.get("extraction") or []) and offer.get("title"):
            offer.setdefault("url", url)
            items.append(offer)
    items = websearch.wttj_keyword_filter(items, metiers_for(profile))
    # Les pages isolées gardent la règle d'âge : une page encore accessible
    # n'est pas une offre encore affichée par l'entreprise.
    items = [i for i in websearch.drop_expired(items)
             if i.get("_live") or websearch.drop_stale([i], profile)]
    items = websearch.drop_abroad(items, profile, strict=True)
    items = websearch.rank_by_place_and_date(items, profile)
    cap = max(1, websearch._env_int("JOBWATCH_WEB_MAX_PER_SOURCE", 50))
    out = []
    for item in items[:cap]:
        rec = normalize(item, "search_engine")
        if item.get("_live"):
            rec["_live"] = True
        out.append(rec)
    return out
