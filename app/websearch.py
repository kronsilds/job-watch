"""Veille web « fair use » sur les pages de recherche classiques des sites emploi.

Usage personnel, quelques requêtes par jour :

* **User-Agent honnête**, délai aléatoire entre requêtes, **quota quotidien**,
  **cache disque** avec TTL, et arrêt poli sur 403/429/captcha (aucun
  contournement des protections techniques).
* **robots.txt** est consulté et journalisé. Politique
  ``JOBWATCH_WEB_ROBOTS_POLICY`` : ``fair`` (défaut — la page est chargée à
  volume minimal même si robots.txt est défavorable, avertissement journalisé)
  ou ``strict`` (une page interdite par robots.txt est refusée). Les refus
  techniques (403/429) restent toujours respectés.
* Recherche **matrice métier × zone × source** (comme le workflow n8n
  ``job-agent``) : chaque métier cible est croisé avec chaque zone résolue via
  ``geo.api.gouv.fr`` et chaque source HTML (France Travail, HelloWork,
  LinkedIn, Adzuna, Indeed). Une URL en échec n'abandonne jamais les autres :
  chaque requête produit un diagnostic (``ok / no_results / blocked /
  rate_limited / http_error / network_error / empty_response / parse_error``).
  Exception : WTTJ, dont le robots.txt interdit ``*/jobs?query=*`` — on
  utilise son **sitemap officiel** (fourni pour les crawlers).
* France Travail et Adzuna préfèrent leur API officielle quand les jetons
  sont configurés (voir :mod:`app.scrapers`) ; la page de recherche HTML
  sert de repli sans clés.

Les parseurs sont des fonctions pures ``parse_*(texte) -> list[dict]``,
testables sans réseau. Les erreurs héritent de :class:`FairUseError` et
remontent dans le rapport par source sans bloquer les autres connecteurs.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import random
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - dépendance listée dans requirements
    BeautifulSoup = None

from .config import BASE_DIR

logger = logging.getLogger("jobwatch.websearch")

DEFAULT_UA = ("JobWatch/0.3 (personal job-search watcher; non-commercial; "
              "a few requests per day) python-httpx")

# Sources HTML couvertes par la matrice métier × zone (aligné sur le workflow
# n8n « job-agent » : 5 sources par défaut, APEC/WTTJ en opt-in car fragiles).
MATRIX_SOURCES = ["france_travail", "hellowork", "linkedin", "adzuna", "indeed"]

# Nombre max d'offres conservées par page de résultats (garde-fou n8n : 25).
MAX_OFFERS_PER_PAGE = 25

# Plafond de requêtes préparées par veille (garde-fou n8n : 200).
MAX_SEARCH_REQUESTS = 200


def _env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    return default if val in (None, "") else val


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def user_agent() -> str:
    return _env("JOBWATCH_WEB_UA", DEFAULT_UA) or DEFAULT_UA


def cache_dir() -> Path:
    p = Path(_env("JOBWATCH_WEB_CACHE_DIR", str(BASE_DIR / "data" / "webcache")))
    p.mkdir(parents=True, exist_ok=True)
    return p


# Hook de test : transport httpx injectable (MockTransport) ; None = réel.
_TRANSPORT: httpx.BaseTransport | None = None


class FairUseError(RuntimeError):
    """Erreur de veille web non bloquante (signalée dans le rapport)."""


class BudgetExceeded(FairUseError):
    def __init__(self):
        super().__init__(
            "quota quotidien de requêtes web atteint — relancez demain ou "
            "augmentez JOBWATCH_WEB_DAILY_LIMIT")


class RobotsBlocked(FairUseError):
    def __init__(self, url: str):
        super().__init__(
            f"robots.txt de {urlparse(url).netloc} interdit cette page — non contourné")


class SiteBlocked(FairUseError):
    def __init__(self, netloc: str, detail: str = ""):
        super().__init__(
            f"{netloc} a refusé la requête {detail}— protection anti-bot "
            "respectée, aucun contournement")


# ---------------------------------------------------------------------------
# Cache disque + quota quotidien + politesse
# ---------------------------------------------------------------------------

def _cache_paths(key: str) -> tuple[Path, Path]:
    return cache_dir() / f"{key}.body", cache_dir() / f"{key}.meta"


def _cache_get(key: str, ttl: int) -> str | None:
    body, meta = _cache_paths(key)
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        if time.time() - float(info.get("ts", 0)) < ttl and body.exists():
            return body.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return None


def _cache_set(key: str, text: str, final_url: str, status: int) -> None:
    body, meta = _cache_paths(key)
    try:
        body.write_text(text, encoding="utf-8")
        meta.write_text(json.dumps({"ts": time.time(), "url": final_url,
                                    "status": status}), encoding="utf-8")
    except OSError:
        pass


def _consume_budget() -> None:
    """Décompte une requête sortante ; lève BudgetExceeded au-delà du quota."""
    limit = _env_int("JOBWATCH_WEB_DAILY_LIMIT", 30)
    path = cache_dir() / f"usage-{date.today():%Y%m%d}.json"
    count = 0
    try:
        count = int(json.loads(path.read_text(encoding="utf-8")).get("count", 0))
    except (OSError, ValueError):
        pass
    if count >= limit:
        raise BudgetExceeded()
    try:
        path.write_text(json.dumps({"count": count + 1}), encoding="utf-8")
    except OSError:
        pass


def _polite_sleep() -> None:
    lo = _env_float("JOBWATCH_WEB_DELAY_MIN", 2.0)
    hi = _env_float("JOBWATCH_WEB_DELAY_MAX", 5.0)
    delay = random.uniform(min(lo, hi), max(lo, hi))
    if delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------------------
# robots.txt (cache mémoire 24 h ; indisponibilité réseau => rien d'interdit)
# ---------------------------------------------------------------------------

_ROBOTS_TTL = 86400
_robots_cache: dict[str, tuple[float, RobotFileParser | None]] = {}


def _make_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9",
        },
        timeout=_env_float("JOBWATCH_WEB_TIMEOUT", 25.0),
        follow_redirects=True,
        transport=_TRANSPORT,
    )


def robots_allowed(url: str) -> bool:
    netloc = urlparse(url).netloc
    now = time.time()
    cached = _robots_cache.get(netloc)
    if cached and now - cached[0] < _ROBOTS_TTL:
        parser = cached[1]
    else:
        parser = None
        robots_url = f"{urlparse(url).scheme}://{netloc}/robots.txt"
        client = _make_client()
        try:
            r = client.get(robots_url, timeout=15)
            if r.status_code == 200:
                parser = RobotFileParser()
                parser.parse(r.text.splitlines())
            # 4xx/5xx : pas de robots.txt exploitable => rien n'est interdit
        except httpx.HTTPError:
            parser = None
        finally:
            client.close()
        _robots_cache[netloc] = (now, parser)
    if parser is None:
        return True
    ua_token = user_agent().split("/")[0]
    return parser.can_fetch(ua_token, url) and parser.can_fetch("*", url)


def _robots_gate(url: str) -> None:
    """Applique la politique robots.txt (JOBWATCH_WEB_ROBOTS_POLICY).

    * ``strict`` : une page interdite par robots.txt est refusée (RobotsBlocked).
    * ``fair`` (défaut, usage personnel quelques requêtes/jour) : la page est
      tout de même chargée — en conservant délai, quota, cache et User-Agent
      honnête — mais un avertissement est journalisé ; les refus *techniques*
      (403/429/captcha) restent eux toujours respectés, sans contournement.
    """
    if robots_allowed(url):
        return
    policy = (_env("JOBWATCH_WEB_ROBOTS_POLICY", "fair") or "fair").lower()
    if policy == "strict":
        raise RobotsBlocked(url)
    logger.warning(
        "robots.txt de %s défavorable pour %s — accès maintenu en mode fair "
        "(usage personnel, volume minimal) ; mettez "
        "JOBWATCH_WEB_ROBOTS_POLICY=strict pour bloquer.",
        urlparse(url).netloc, url)


# ---------------------------------------------------------------------------
# fetch_page : LE point de sortie réseau (cache -> robots -> quota -> délai)
# ---------------------------------------------------------------------------

def fetch_page(url: str, *, method: str = "GET", json_body: dict | None = None,
               ttl: int | None = None, headers: dict | None = None) -> str:
    """Récupère une page (HTML/JSON/XML) en respectant la charte fair use."""
    if ttl is None:
        ttl = _env_int("JOBWATCH_WEB_CACHE_TTL", 21600)
    payload = json.dumps(json_body, sort_keys=True) if json_body else ""
    key = hashlib.sha256(f"{method}|{url}|{payload}".encode()).hexdigest()[:24]
    if method == "GET":
        cached = _cache_get(key, ttl)
        if cached is not None:
            return cached
    _robots_gate(url)
    _consume_budget()
    _polite_sleep()
    client = _make_client()
    try:
        r = client.request(method, url, json=json_body, headers=headers or {})
    except httpx.HTTPError as exc:
        raise FairUseError(
            f"erreur réseau vers {urlparse(url).netloc}: {type(exc).__name__}") from exc
    finally:
        client.close()
    if r.status_code in (401, 403, 407, 429, 451):
        raise SiteBlocked(urlparse(url).netloc, f"(HTTP {r.status_code}) ")
    r.raise_for_status()
    content = r.content
    if content[:2] == b"\x1f\x8b":  # fichier .gz (sitemaps WTTJ)
        content = gzip.decompress(content)
    text = content.decode(r.encoding or "utf-8", errors="replace")
    if method == "GET":
        _cache_set(key, text, str(r.url), r.status_code)
    return text


def _require_bs4() -> None:
    if BeautifulSoup is None:
        raise FairUseError(
            "beautifulsoup4 est requis pour le parsing HTML (pip install beautifulsoup4)")


def _text(el) -> str:
    """Texte propre d'un élément BeautifulSoup ('' si None)."""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


# ---------------------------------------------------------------------------
# Zones de recherche : résolution des lieux via geo.api.gouv.fr (n8n)
# ---------------------------------------------------------------------------

GEO_API_BASE = "https://geo.api.gouv.fr"


def _norm_key(s: str) -> str:
    """Clé de comparaison insensible aux accents/casse/ponctuation (n8n)."""
    s = _strip_accents(str(s or "")).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _split_profile_list(value) -> list[str]:
    """Découpe une valeur de profil (str ou liste) en entrées uniques."""
    if isinstance(value, str):
        parts = re.split(r"[,;\n]+", value)
    elif isinstance(value, (list, tuple)):
        parts = []
        for entry in value:
            parts.extend(re.split(r"[,;\n]+", str(entry or "")))
    else:
        parts = []
    out: list[str] = []
    for part in parts:
        part = str(part or "").strip()
        if part and part not in out:
            out.append(part)
    return out


def _fetch_geo_json(url: str, ttl: int = 86400) -> list:
    """Référentiel geo.api.gouv.fr (cache long + quota partagé).

    Indisponible → [] (dégradation en recherche France + avertissement,
    comme n8n) au lieu de lever.
    """
    try:
        text = fetch_page(url, ttl=ttl)
    except (FairUseError, Exception):
        return []
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return []
    return body if isinstance(body, list) else []


def resolve_zones(profile: dict) -> tuple[list[dict], list[str]]:
    """Résout les lieux du profil en zones (label + code FT).

    Chaque zone porte ``{"label", "ft", "remote"}`` : ``ft`` vaut ``75D``
    (département), ``11R`` (région), un code INSEE de commune, ``""``
    (France entière) ou ``None`` (non résolu → FT ignorée pour la zone).
    """
    from .scrapers import locations_for  # import différé (cycle)
    locations = _split_profile_list(
        profile.get("localisation") or profile.get("locations")
        or locations_for(profile))
    departments = _fetch_geo_json(
        f"{GEO_API_BASE}/departements?fields=nom,code")
    regions = _fetch_geo_json(f"{GEO_API_BASE}/regions?fields=nom,code")
    warnings: list[str] = []
    zones: list[dict] = []
    for location in locations:
        key = _norm_key(location)
        if key in ("france", "franceentiere", "national"):
            zones.append({"label": "France", "ft": "", "remote": False})
            continue
        name = re.sub(r"\s*\((?:\d{2,5}|2[AB])\)\s*$", "", location,
                      flags=re.I).strip()
        hint_m = re.search(r"\((\d{2,5}|2[AB])\)\s*$", location, flags=re.I)
        hint = hint_m.group(1).upper() if hint_m else ""
        dep = next((d for d in departments
                    if _norm_key(d.get("nom")) == _norm_key(name)
                    or str(d.get("code") or "").upper() == name.upper().rstrip("D")),
                   None)
        reg = next((r for r in regions
                    if _norm_key(r.get("nom")) == _norm_key(name)
                    or (str(r.get("code") or "").upper() + "R") == name.upper()),
                   None)
        if dep:
            zones.append({"label": str(dep.get("nom")),
                          "ft": f"{dep.get('code')}D", "remote": False})
            continue
        if reg:
            zones.append({"label": str(reg.get("nom")),
                          "ft": f"{reg.get('code')}R", "remote": False})
            continue
        param = "codePostal" if re.fullmatch(r"\d{5}", name) else "nom"
        communes = _fetch_geo_json(
            f"{GEO_API_BASE}/communes?{param}={quote_plus(name)}"
            "&fields=nom,code,codeDepartement,codesPostaux&limit=20")
        matches = [c for c in communes
                   if _norm_key(c.get("nom")) == _norm_key(name)
                   or name in (c.get("codesPostaux") or [])]
        if hint:
            matches = [c for c in matches
                       if c.get("codeDepartement") == hint
                       or c.get("code") == hint
                       or hint in (c.get("codesPostaux") or [])]
        if len(matches) == 1:
            zones.append({"label": f"{matches[0].get('nom')}, France",
                          "ft": str(matches[0].get("code")), "remote": False})
        else:
            zones.append({"label": name, "ft": None, "remote": False})
            warnings.append("France Travail : lieu non résolu ou ambigu, "
                            f"recherche ignorée pour « {location} ».")
    if not zones:
        zones.append({"label": "France", "ft": "", "remote": False})
    mobility = _norm_key(profile.get("mobilite") or profile.get("mobility") or "")
    if re.search(r"fullremote|100teletravail|teletravailcomplet|teletravailintegral",
                 mobility):
        zones.append({"label": "France", "ft": "", "remote": True})
    return zones, warnings


def _metiers_from_profile(profile: dict) -> list[str]:
    from .scrapers import metiers_for  # import différé (cycle)
    return _split_profile_list(
        profile.get("metiers_cibles") or metiers_for(profile) or [])


# ---------------------------------------------------------------------------
# URLs de recherche « classiques » par site
# ---------------------------------------------------------------------------

def _query_and_location(profile: dict) -> tuple[str, str]:
    from .scrapers import locations_for, metiers_for  # import différé (cycle)
    q = " ".join(metiers_for(profile))
    locs = locations_for(profile)
    return q, (locs[0] if locs else "")


def _build_url(base: str, params: dict) -> str:
    from urllib.parse import urlencode
    qs = urlencode([(k, v) for k, v in params.items()
                    if v != "" and v is not None])
    return f"{base}?{qs}" if qs else base


def linkedin_search_url(profile: dict, query: str | None = None,
                        location: str | None = None,
                        remote: bool = False) -> str:
    if query is None or location is None:
        q, loc = _query_and_location(profile)
        query = q if query is None else query
        location = loc if location is None else location
    params = {"keywords": query or "", "location": location or ""}
    if remote:
        params["f_WT"] = "2"  # filtre télétravail LinkedIn (comme n8n)
    return _build_url("https://www.linkedin.com/jobs/search", params)


def indeed_search_url(profile: dict, query: str | None = None,
                      location: str | None = None) -> str:
    if query is None or location is None:
        q, loc = _query_and_location(profile)
        query = q if query is None else query
        location = loc if location is None else location
    place = "" if (location or "") == "France" else (location or "")
    return _build_url("https://fr.indeed.com/jobs",
                      {"q": query or "", "l": place})


def hellowork_search_url(profile: dict, query: str | None = None,
                         location: str | None = None) -> str:
    if query is None or location is None:
        q, loc = _query_and_location(profile)
        query = q if query is None else query
        location = loc if location is None else location
    place = (location or "").replace(", France", "")
    place = "" if (location or "") == "France" else place
    return _build_url("https://www.hellowork.com/fr-fr/emploi/recherche.html",
                      {"k": query or "", "l": place})


def adzuna_search_url(profile: dict, query: str | None = None,
                      location: str | None = None) -> str:
    if query is None or location is None:
        q, loc = _query_and_location(profile)
        query = q if query is None else query
        location = loc if location is None else location
    place = (location or "").replace(", France", "")
    place = "" if (location or "") == "France" else place
    return _build_url("https://www.adzuna.fr/search",
                      {"q": query or "", "w": place})


def francetravail_search_url(profile: dict, query: str | None = None,
                             ft_code: str | None = None) -> str:
    """URL FT avec *code lieu* (75D/11R/INSEE), rayon et partenaires (n8n)."""
    if query is None and ft_code is None:
        q, loc = _query_and_location(profile)
        url = ("https://candidat.francetravail.fr/offres/recherche"
               f"?motsCles={quote_plus(q)}")
        if loc:
            url += f"&lieux={quote_plus(loc)}"
        return url
    params = {"motsCles": query or "", "lieux": ft_code or "",
              "rayon": 10, "offresPartenaires": "true", "tri": 0}
    return _build_url("https://candidat.francetravail.fr/offres/recherche",
                      params)


APEC_SEARCH_ENDPOINT = "https://www.apec.fr/cms/webservices/rechercheOffre"
APEC_DETAIL_URL = "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{}"
WTTJ_SITEMAP_INDEX = "https://www.welcometothejungle.com/sitemaps/index.xml.gz"


def build_search_requests(profile: dict, sources: list[str] | None = None,
                          zones: list[dict] | None = None,
                          warnings: list[str] | None = None
                          ) -> tuple[list[dict], list[str]]:
    """Matrice métier × zone × source (préparation n8n « Préparer URLs »).

    Chaque requête porte ``{source, url, query, location, remote_search}``.
    France Travail est ignorée pour les zones non résolues (``ft=None``).
    Lève :class:`FairUseError` si aucun métier ou aucune recherche possible,
    et plafonne à ``MAX_SEARCH_REQUESTS`` (200, garde-fou n8n).
    """
    from .scrapers import sources_for  # import différé (cycle)
    metiers = _metiers_from_profile(profile)
    if not metiers:
        raise FairUseError("aucun métier cible dans le profil pour la veille web")
    prep_warnings: list[str] = list(warnings or [])
    if zones is None:
        zones, zone_warnings = resolve_zones(profile)
        prep_warnings.extend(zone_warnings)
    wanted = [str(s).strip().lower()
              for s in (sources if sources is not None else sources_for(profile))]
    known = [s for s in wanted if s in MATRIX_SOURCES]
    for raw in wanted:
        if raw and raw not in MATRIX_SOURCES and raw not in ("apec", "wttj"):
            prep_warnings.append(f"Source inconnue ignorée : {raw}")
    if not known:
        raise FairUseError("aucune source HTML reconnue : " + " ".join(prep_warnings))
    builders = {
        "france_travail": lambda q, z: francetravail_search_url(profile, q, z["ft"]),
        "hellowork": lambda q, z: hellowork_search_url(profile, q, z["label"]),
        "linkedin": lambda q, z: linkedin_search_url(profile, q, z["label"], z["remote"]),
        "adzuna": lambda q, z: adzuna_search_url(profile, q, z["label"]),
        "indeed": lambda q, z: indeed_search_url(profile, q, z["label"]),
    }
    out: list[dict] = []
    seen: set[str] = set()
    for metier in metiers:
        for zone in zones:
            for source in known:
                if source == "france_travail" and zone.get("ft") is None:
                    continue
                query = metier + (" télétravail" if zone.get("remote") else "")
                url = builders[source](query, zone)
                if url in seen:
                    continue
                seen.add(url)
                out.append({"source": source, "url": url, "query": query,
                            "location": zone.get("label") or "",
                            "remote_search": bool(zone.get("remote"))})
    if not out:
        raise FairUseError("aucune recherche possible : " + " ".join(prep_warnings))
    if len(out) > MAX_SEARCH_REQUESTS:
        raise FairUseError(
            f"{len(out)} recherches préparées : limitez les métiers ou zones "
            f"pour rester sous {MAX_SEARCH_REQUESTS} requêtes.")
    return out, prep_warnings



# ---------------------------------------------------------------------------
# Parseurs purs (texte -> dicts bruts normalisables), testables sans réseau
# ---------------------------------------------------------------------------

def _html_to_text(raw: str) -> str:
    """Nettoie un fragment HTML : entités + balises → texte simple (n8n)."""
    import html as _html
    text = re.sub(r"<[^>]+>", " ", str(raw or ""))
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _first_group(pattern: str, text: str, flags: int = re.I | re.S) -> str:
    m = re.search(pattern, text, flags)
    return m.group(1) if m else ""


def parse_linkedin(html: str, base: str = "https://www.linkedin.com") -> list[dict]:
    """Cartes SSR de la page de recherche invités LinkedIn.

    Parseur primaire strict (bs4) + repli regex tolérant façon n8n quand le
    markup dérive (split ``<li`` / ``base-card`` au lieu des sélecteurs CSS).
    """
    _require_bs4()
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("div.base-search-card"):
        link = card.select_one("a.base-card__full-link")
        title = _text(card.select_one("h3.base-search-card__title"))
        if not link or not title:
            continue
        href = link.get("href", "").split("?")[0]  # retire les params de tracking
        out.append({
            "title": title,
            "company": _text(card.select_one("h4.base-search-card__subtitle")) or "Inconnue",
            "location": _text(card.select_one("span.job-search-card__location")),
            "url": href if href.startswith("http") else urljoin(base, href),
            "description": "",
        })
    if out:
        return out
    return _parse_linkedin_loose(html, base)


def _parse_linkedin_loose(html: str, base: str) -> list[dict]:
    """Repli regex n8n : split <li / base-card, motifs title/link/entreprise."""
    out = []
    for chunk in re.split(r"<li|<div class=\"base-card", html)[1:]:
        if "base-search-card" not in chunk and "base-card" not in chunk:
            continue
        title = _html_to_text(_first_group(
            r"base-search-card__title[^>]*>([\s\S]*?)</", chunk)) or _html_to_text(
            _first_group(r"sr-only[^>]*>([\s\S]*?)</", chunk))
        href = (_first_group(r"base-card__full-link[^>]*href=\"([^\"]+)\"", chunk)
                or _first_group(r"href=\"(https://[^\"]*/jobs/view/[^\"]+)\"", chunk))
        if not title or not href:
            continue
        company = (_html_to_text(_first_group(
            r"base-search-card__subtitle[^>]*>([\s\S]*?)</h4>", chunk))
            or _html_to_text(_first_group(
                r"hidden-nested-link[^>]*>([\s\S]*?)</", chunk)) or "Inconnue")
        out.append({
            "title": title,
            "company": company,
            "location": _html_to_text(_first_group(
                r"job-search-card__location[^>]*>([\s\S]*?)</span>", chunk)),
            "url": href.split("?")[0],
            "description": "",
        })
    return out


def parse_indeed(html: str, base: str = "https://fr.indeed.com") -> list[dict]:
    """Cartes Indeed (si la page n'est pas bloquée par l'anti-bot)."""
    _require_bs4()
    low = html.lower()
    if "captcha" in low or "unusual traffic" in low or "px-captcha" in low:
        raise SiteBlocked("fr.indeed.com", "(page de vérification anti-bot) ")
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.job_seen_beacon") or soup.select("td.resultContent")
    out = []
    for card in cards:
        link = card.select_one('h2.jobTitle a[href*="jk="]') or card.select_one('a[href*="jk="]')
        title = _text(card.select_one("h2.jobTitle")) or _text(link)
        if not link or not title:
            continue
        href = link.get("href", "")
        m = re.search(r"jk=([a-zA-Z0-9]+)", href)  # ne garde que l'identifiant d'offre
        if m:
            href = f"/viewjob?jk={m.group(1)}"
        out.append({
            "title": title,
            "company": (_text(card.select_one('[data-testid="company-name"]'))
                        or _text(card.select_one("span.companyName"))),
            "location": (_text(card.select_one('[data-testid="text-location"]'))
                         or _text(card.select_one("div.companyLocation"))),
            "url": href if href.startswith("http") else urljoin(base, href),
            "description": "",
        })
    if out:
        return out
    return _parse_indeed_loose(html)


def _parse_indeed_loose(html: str) -> list[dict]:
    """Repli regex n8n : split job_seen_beacon / data-jk, reconstruction jk."""
    out = []
    for chunk in re.split(r"job_seen_beacon|data-jk=", html)[1:]:
        jk = (_first_group(r"^\"?([a-zA-Z0-9]{10,})\"", chunk, re.S)
              or _first_group(r"jk=([a-zA-Z0-9]+)", chunk))
        title = (_html_to_text(_first_group(r"jobTitle[\s\S]*?>([\s\S]*?)</a>", chunk))
                 or _first_group(r"title=\"([^\"]+)\"", chunk)
                 or _html_to_text(_first_group(r"<h2[^>]*>([\s\S]*?)</h2>", chunk)))
        if not jk or not title:
            continue
        out.append({
            "title": title,
            "company": (_html_to_text(_first_group(r"company-name[^>]*>([\s\S]*?)</", chunk))
                        or _html_to_text(_first_group(r"companyName[^>]*>([\s\S]*?)</", chunk))
                        or "Inconnue"),
            "location": (_html_to_text(_first_group(r"text-location[^>]*>([\s\S]*?)</", chunk))
                         or _html_to_text(_first_group(r"companyLocation[^>]*>([\s\S]*?)</", chunk))),
            "url": f"https://fr.indeed.com/viewjob?jk={jk}",
            "description": "",
        })
    return out


def parse_hellowork(html: str, base: str = "https://www.hellowork.com") -> list[dict]:
    """Cartes SSR HelloWork : inputs cachés title/company/offerId + tags."""
    _require_bs4()
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("li[data-id-storage-item-id]"):
        def hidden(name: str) -> str:
            el = card.select_one(f'input[type="hidden"][name="{name}"]')
            return (el.get("value") or "").strip() if el else ""
        title, company, offer_id = hidden("title"), hidden("company"), hidden("offerId")
        if not title or not offer_id:
            continue
        link = card.select_one('a[href*="/emplois/"]')
        href = link.get("href", "") if link else f"/fr-fr/emplois/{offer_id}.html"
        out.append({
            "title": title,
            "company": company or "Inconnue",
            "location": _text(card.select_one('[data-cy="localisationCard"]')),
            "contract": _text(card.select_one('[data-cy="contractCard"]')),
            "url": href if href.startswith("http") else urljoin(base, href),
            "description": "",
        })
    if out:
        return out
    return _parse_hellowork_loose(html, base)


def _hidden_input(chunk: str, name: str) -> str:
    m = re.search(r"name=\"" + re.escape(name) + r"\"[^>]*value=\"([^\"]*)\"",
                  chunk, re.I)
    if not m:
        m = re.search(r"value=\"([^\"]*)\"[^>]*name=\"" + re.escape(name) + r"\"",
                      chunk, re.I)
    import html as _html
    return _html.unescape(m.group(1)).strip() if m else ""


def _parse_hellowork_loose(html: str, base: str) -> list[dict]:
    """Repli regex n8n : inputs cachés title/company/offerId + tags."""
    out = []
    for chunk in html.split("<li")[1:]:
        if "data-id-storage-item-id" not in chunk:
            continue
        title = _hidden_input(chunk, "title")
        offer_id = _hidden_input(chunk, "offerId")
        if not title or not offer_id:
            continue
        href = (_first_group(r"href=\"([^\"]*/emplois/[^\"]*)\"", chunk)
                or f"/fr-fr/emplois/{offer_id}.html")
        out.append({
            "title": title,
            "company": _hidden_input(chunk, "company") or "Inconnue",
            "location": _html_to_text(_first_group(
                r"localisationCard[^>]*>([\s\S]*?)</", chunk)),
            "contract": _html_to_text(_first_group(
                r"contractCard[^>]*>([\s\S]*?)</", chunk)),
            "url": href if href.startswith("http") else urljoin(base, href),
            "description": "",
        })
    return out



def parse_adzuna_html(html: str) -> list[dict]:
    """Cartes <article data-aid="..."> de la page de recherche HTML d'Adzuna."""
    _require_bs4()
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("article[data-aid]"):
        link = card.select_one('a[data-js="jobLink"]')
        if not link:
            continue
        title_el = card.select_one("h2")
        title = _text(title_el) if title_el else ""
        if not title:
            continue
        href = link.get("href", "")
        loc_el = card.select_one(".ui-location")
        company_el = card.select_one(".ui-company")
        desc_el = card.select_one(".max-snippet-height, span.max-snippet-height")
        out.append({
            "title": title,
            "company": _text(company_el) if company_el else "Inconnue",
            "location": _text(loc_el) if loc_el else "",
            "url": href if href.startswith("http") else urljoin("https://www.adzuna.fr", href),
            "description": _text(desc_el) if desc_el else "",
        })
    if out:
        return out
    return _parse_adzuna_loose(html)

def _parse_adzuna_loose(html: str) -> list[dict]:
    """Repli regex n8n : split <article, motifs data-aid/h2/jobLink."""
    out = []
    for chunk in html.split("<article")[1:]:
        if "data-aid" not in chunk:
            continue
        title = _html_to_text(_first_group(r"<h2[^>]*>([\s\S]*?)</h2>", chunk))
        if not title:
            continue
        href = (_first_group(r"jobLink\"[^>]*href=\"([^\"]+)\"", chunk)
                or _first_group(r"href=\"([^\"]+)\"", chunk))
        out.append({
            "title": title,
            "company": _html_to_text(_first_group(r"ui-company[^>]*>([\s\S]*?)</", chunk)) or "Inconnue",
            "location": _html_to_text(_first_group(r"ui-location[^>]*>([\s\S]*?)</", chunk)),
            "url": href,
            "description": _html_to_text(_first_group(
                r"max-snippet-height[^>]*>([\s\S]*?)</", chunk)),
        })
    return out



def parse_francetravail_html(html: str,
                             base: str = "https://candidat.francetravail.fr") -> list[dict]:
    """Liste SSR (li.result) de la page recherche/SEO France Travail."""
    _require_bs4()
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("li.result[data-id-offre]"):
        titre_el = card.select_one("h2[data-intitule-offre]")
        if not titre_el:
            continue
        link = card.select_one('a[href*="/offres/recherche/detail/"]')
        subtext = card.select_one("p.subtext")
        company, location = "", ""
        if subtext is not None:
            span = subtext.select_one("span")
            if span is not None:
                location = _text(span)
                span.extract()
            company = _text(subtext).removesuffix("-").strip(" -")
        contrat_el = card.select_one("div.media-right p.contrat") or card.select_one("p.contrat")
        desc_el = card.select_one("p.description")
        href = link.get("href", "") if link else ""
        out.append({
            "title": _text(titre_el),
            "company": company or "Inconnue",
            "location": location,
            "contract": _text(contrat_el) if contrat_el else "",
            "url": href if href.startswith("http") else urljoin(base, href),
            "description": _text(desc_el) if desc_el else "",
        })
    if out:
        return out
    return _parse_francetravail_loose(html, base)

def _split_fr_company_location(raw: str) -> tuple[str, str]:
    """Découpe « ENTREPRISE - 75 - VILLE » au niveau du code postal (n8n)."""
    parts = [p.strip() for p in re.split(r"\s+-\s+", raw.strip()) if p.strip()]
    if len(parts) < 2:
        return (raw.strip() or "Inconnue", "")
    idx = next((k for k, p in enumerate(parts)
                if re.fullmatch(r"\d{2,3}[abAB]?", p)), -1)
    if idx > 0:
        return (" - ".join(parts[:idx]) or "Inconnue", " - ".join(parts[idx:]))
    return (" - ".join(parts[:-1]) or "Inconnue", parts[-1])


def _is_contract_text(s: str) -> bool:
    return bool(re.search(
        r"cdi|cdd|stage|altern|apprentiss|int[eé]rim|freelance|lib[eé]ral|"
        r"temps\s|vacation|saisonnier", str(s or ""), re.I))


def _parse_francetravail_loose(html: str, base: str) -> list[dict]:
    """Repli regex n8n : split <li, data-id-offre, h2/subtext/contrat."""
    out = []
    for chunk in html.split("<li")[1:]:
        if "data-id-offre" not in chunk:
            continue
        title = (_html_to_text(_first_group(
            r"data-intitule-offre[^>]*>([\s\S]*?)</h2>", chunk))
            or _html_to_text(_first_group(r"<h2[^>]*>([\s\S]*?)</h2>", chunk)))
        if not title:
            continue
        company, location = _split_fr_company_location(_html_to_text(
            _first_group(r"subtext[^>]*>([\s\S]*?)</p>", chunk)))
        contract = _html_to_text(
            _first_group(r"contrat[^>]*>([\s\S]*?)</p>", chunk))
        if not _is_contract_text(contract):
            contract = ""
        out.append({
            "title": title,
            "company": company,
            "location": location,
            "contract": contract,
            "url": _first_group(
                r"href=\"([^\"]*/offres/recherche/detail/[^\"]*)\"", chunk),
            "description": _html_to_text(_first_group(
                r"description[^>]*>([\s\S]*?)</p>", chunk)),
        })
    return out


def parse_structured(html: str) -> list[dict]:
    """Repli universel n8n : blocs JSON-LD ``@type: JobPosting``."""
    out = []
    for m in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>',
            html, re.I):
        try:
            payload = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value)
                continue
            if not isinstance(value, dict):
                continue
            stack.extend(value.values())
            types = value.get("@type")
            seq = types if isinstance(types, list) else [types]
            if "JobPosting" not in seq:
                continue
            title = _html_to_text(value.get("title") or "")
            url = str(value.get("url") or "").strip()
            if not title or not url:
                continue
            locs = value.get("jobLocation")
            seq_loc = locs if isinstance(locs, list) else [locs]
            places = []
            for loc in seq_loc:
                addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
                place = ", ".join(p for p in (
                    addr.get("addressLocality"), addr.get("addressRegion")) if p)
                if place:
                    places.append(place)
            hiring = value.get("hiringOrganization") or {}
            out.append({
                "title": title,
                "company": _html_to_text(hiring.get("name") or "") or "Inconnue",
                "location": " / ".join(places),
                "contract": str(value.get("employmentType") or ""),
                "url": url,
                "description": _html_to_text(value.get("description") or ""),
            })
    return out



_CHALLENGE_RE = re.compile(
    r"security check|additional verification required|verify you are human|"
    r"just a moment|access denied|unusual traffic|captcha", re.I)
_NO_RESULTS_RE = re.compile(
    r"aucun(?:e)? (?:offre|emploi|r[ée]sultat)|\b0\s+offres?\b|"
    r"no (?:jobs|results|matching jobs)", re.I)


def detect_page_challenge(html: str) -> bool:
    """Détecte une page anti-bot / captcha (titre + h1/h2 + iframe, n8n)."""
    title = _html_to_text(_first_group(r"<title[^>]*>([\s\S]*?)</title>", html))
    headings = " ".join(_html_to_text(h) for h in re.findall(
        r"<h[12][^>]*>([\s\S]*?)</h[12]>", html, re.I))
    if _CHALLENGE_RE.search(f"{title} {headings}"):
        return True
    return bool(re.search(
        r"<iframe[^>]*src=[\"'][^\"']*(?:captcha-delivery\.com|"
        r"challenges\.cloudflare\.com)", html, re.I))


def detect_no_results(html: str) -> bool:
    """Détecte une page « 0 résultat » (≠ parse_error, comme n8n)."""
    title = _html_to_text(_first_group(r"<title[^>]*>([\s\S]*?)</title>", html))
    headings = " ".join(_html_to_text(h) for h in re.findall(
        r"<h[12][^>]*>([\s\S]*?)</h[12]>", html, re.I))
    return bool(_NO_RESULTS_RE.search(f"{title} {headings}"))


_BASE_HOSTS = {"france_travail": "candidat.francetravail.fr",
               "hellowork": "www.hellowork.com",
               "linkedin": "www.linkedin.com",
               "adzuna": "www.adzuna.fr",
               "indeed": "fr.indeed.com"}


def normalize_offer_url(url: str, source: str) -> str:
    """Normalise une URL d'offre (// et / relatifs → absolus, comme n8n)."""
    import html as _html
    url = _html.unescape(str(url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://" + _BASE_HOSTS.get(source, "www.google.com") + url
    if re.match(r"^https?://[^/\s]+(?:/|$)", url, re.I):
        return url
    return ""


def detect_contract(text: str) -> str:
    """Contrat deviné depuis le texte (titre + description + lieu, n8n)."""
    low = str(text or "").lower()
    if re.search(r"altern|apprentiss", low):
        return "alternance"
    if re.search(r"stage|intern", low):
        return "stage"
    if re.search(r"freelance|ind[eé]pendant|portage", low):
        return "freelance"
    if re.search(r"int[eé]rim", low):
        return "interim"
    if re.search(r"\bcdd\b", low):
        return "cdd"
    if re.search(r"\bcdi\b", low):
        return "cdi"
    return ""


def detect_remote(text: str) -> str:
    """Modalité remote devinée depuis le texte (n8n)."""
    low = str(text or "").lower()
    found = []
    if re.search(r"t[eé]l[eé]travail|remote", low):
        found.append("remote")
    if re.search(r"hybride|hybrid", low):
        found.append("hybride")
    if re.search(r"sur site|on ?site|pr[eé]sentiel", low):
        found.append("sur site")
    return ", ".join(found)


def detect_salary_range(text: str) -> tuple[int | None, int | None]:
    """Fourchette de salaire devinée depuis le texte (en €, comme n8n)."""
    vals: list[int] = []
    for m in re.finditer(r"(\d{2,3}(?:[ \u202f.,]\d{3})+)\s*€", str(text or "")):
        vals.append(int(re.sub(r"\D", "", m.group(1))))
    for m in re.finditer(r"(\d{2,3})\s*k\s*€", str(text or ""), re.I):
        vals.append(int(m.group(1)) * 1000)
    if re.search(r"salaire|r[eé]mun[eé]ration|salary|package|brut",
                 str(text or ""), re.I):
        for m in re.finditer(r"\b(\d{2,3})\s*k\b", str(text or ""), re.I):
            vals.append(int(m.group(1)) * 1000)
    kept = [v for v in vals if 15000 <= v <= 400000]
    if not kept:
        return None, None
    return min(kept), max(kept)


def enrich_raw_offer(raw: dict) -> dict:
    """Complète une offre brute (contrat/remote/salaire, heuristique n8n)."""
    raw = dict(raw or {})
    text = " ".join(str(raw.get(k) or "") for k in ("title", "description", "location"))
    if not raw.get("contract"):
        raw["contract"] = detect_contract(text)
    remote = detect_remote(text)
    if remote:
        raw["remote"] = remote
    if raw.get("salary_min") is None and raw.get("salary_max") is None:
        smin, smax = detect_salary_range(text)
        raw["salary_min"], raw["salary_max"] = smin, smax
    return raw



def parse_apec_json(payload: dict) -> list[dict]:
    """Résultats du webservice rechercheOffre d'APEC (appel de la page recherche)."""
    out = []
    for item in payload.get("resultats") or []:
        numero = str(item.get("numeroOffre") or "")
        titre = str(item.get("intitule") or "")
        if not numero or not titre:
            continue
        salary_min = salary_max = None
        m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*k", str(item.get("salaireTexte") or ""), re.I)
        if m:
            salary_min, salary_max = int(m.group(1)) * 1000, int(m.group(2)) * 1000
        out.append({
            "title": titre,
            "company": item.get("nomCommercial") or "Inconnue",
            "location": item.get("lieuTexte") or "",
            "contract": "",  # typeContrat est un code référentiel côté APEC
            "salary_min": salary_min,
            "salary_max": salary_max,
            "url": APEC_DETAIL_URL.format(numero),
            "description": item.get("texteOffre") or "",
        })
    return out


# ---------------------------------------------------------------------------
# WTTJ : robots.txt interdit */jobs?query=* -> sitemap officiel à la place
# ---------------------------------------------------------------------------

WTTJ_JOB_URL = re.compile(
    r"^https://www\.welcometothejungle\.com/fr/companies/(?P<company>[^/]+)/jobs/"
    r"(?P<slug>[^/?#\s]+)$")


def _looks_like_id(part: str) -> bool:
    """Segment terminal d'un slug WTTJ ressemblant à un identifiant."""
    return bool(re.fullmatch(r"[A-Za-z0-9]{4,12}", part)) and (
        any(ch.isdigit() for ch in part) or part.isupper())


def parse_wttj_sitemap_url(url: str) -> dict | None:
    """Extrait titre/entreprise/ville d'une URL WTTJ ``/fr/companies/.../jobs/...``."""
    m = WTTJ_JOB_URL.match(url.strip())
    if not m:
        return None
    parts = m.group("slug").split("_")
    title = parts[0].replace("-", " ").strip()
    location = ""
    for extra in parts[1:]:
        if not _looks_like_id(extra):
            location = extra.replace("-", " ").strip()
            break
    return {
        "title": title.capitalize(),
        "company": m.group("company").replace("-", " "),
        "location": location,
        "url": m.group(0),
        "description": "",
    }


def wttj_keyword_filter(items: list[dict], metiers: list[str]) -> list[dict]:
    """Garde les offres dont le titre matche tous les mots d'au moins un métier."""
    def tokens(text: str) -> set[str]:
        words = _strip_accents(text.lower()).split()
        return {w for w in (re.sub(r"[^a-z0-9]", "", w) for w in words) if len(w) >= 3}
    metier_tokens = [tokens(m) for m in metiers if m.strip()]
    if not metier_tokens:
        return items
    out = []
    for item in items:
        titre = tokens(item.get("title", ""))
        if any(need and need <= titre for need in metier_tokens):
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Connecteurs (profil -> offres normalisées), un par site
# ---------------------------------------------------------------------------

PARSERS = {
    "linkedin": parse_linkedin,
    "indeed": parse_indeed,
    "hellowork": parse_hellowork,
    "adzuna": parse_adzuna_html,
    "france_travail": parse_francetravail_html,
}


def scrape_request(request: dict) -> tuple[list[dict], dict]:
    """Exécute UNE requête de la matrice et retourne (brutes, diagnostic).

    Port du nœud n8n « Extraire offres » : jamais d'exception réseau (statut
    classé ``network_error / rate_limited / blocked / http_error /
    empty_response``), repli JSON-LD, distinction ``no_results`` vs
    ``parse_error``, cap ``MAX_OFFERS_PER_PAGE``, URLs normalisées et
    enrichissement heuristique contrat/remote/salaire.
    """
    source = request.get("source") or ""
    url = request.get("url") or ""
    diag = {"source": source, "url": url, "query": request.get("query") or "",
            "location": request.get("location") or "", "http_status": None,
            "status": "", "extracted": 0, "kept": 0}
    try:
        html = fetch_page(url)
    except BudgetExceeded as exc:
        diag.update(status="rate_limited", message=str(exc))
        return [], diag
    except (RobotsBlocked, SiteBlocked) as exc:
        diag.update(status="blocked", message=str(exc))
        return [], diag
    except FairUseError as exc:
        diag.update(status="network_error", message=str(exc))
        return [], diag
    except Exception as exc:  # pragma: no cover - garde-fou réseau
        diag.update(status="network_error",
                    message=f"{type(exc).__name__}: {exc}")
        return [], diag
    if not (html or "").strip():
        diag["status"] = "empty_response"
        return [], diag
    challenge = detect_page_challenge(html)
    try:
        raw = list(PARSERS.get(source, parse_structured)(html) or [])
        if not raw:
            raw = parse_structured(html)
        if raw:
            diag["status"] = "ok"
        elif challenge:
            diag["status"] = "blocked"
        elif detect_no_results(html):
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
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        norm_url = normalize_offer_url(item.get("url") or "", source)
        if not norm_url:
            continue
        item["url"] = norm_url
        kept.append(enrich_raw_offer(item))
    diag["kept"] = len(kept)
    if diag["status"] == "ok" and raw and not any(
            o.get("url") for o in raw):
        diag["status"] = "parse_error"
    return kept, diag


def _normalize_all(raw_items: list[dict], source: str) -> list[dict]:
    from .scrapers import normalize  # import différé (cycle)
    cap = max(1, _env_int("JOBWATCH_WEB_MAX_PER_SOURCE", 50))
    return [normalize(item, source) for item in raw_items[:cap]]


def scrape_matrix(profile: dict, sources: list[str] | None = None
                  ) -> tuple[list[dict], dict]:
    """Exécute la matrice métier × zone × source (cœur n8n porté en Python).

    Retourne ``(brutes, {"summary": …, "diagnostics": […]})`` : un diagnostic
    par URL, jamais d'abandon global sur une requête en échec.
    """
    requests, prep_warnings = build_search_requests(profile, sources)
    raws: list[dict] = []
    diagnostics: list[dict] = []
    seen: set[str] = set()
    for request in requests:
        kept, diag = scrape_request(request)
        diagnostics.append(diag)
        for item in kept:
            key = f"{request['source']}|{item.get('title')}|{item.get('company')}".lower()
            if key in seen:
                continue
            seen.add(key)
            item["_matrix_source"] = request["source"]
            raws.append(item)
    summary: dict = {"requests": len(diagnostics), "extracted_unique": len(raws),
                     "incomplete": bool(prep_warnings) or any(
                         d["status"] not in ("ok", "no_results") for d in diagnostics),
                     "warnings": sorted(set(prep_warnings)), "sources": {}}
    for diag in diagnostics:
        slot = summary["sources"].setdefault(
            diag["source"], {"requests": 0, "ok": 0, "no_results": 0,
                             "blocked": 0, "rate_limited": 0, "http_error": 0,
                             "network_error": 0, "empty_response": 0,
                             "parse_error": 0, "offers": 0, "capped": 0})
        slot["requests"] += 1
        slot[diag["status"]] = slot.get(diag["status"], 0) + 1
        slot["offers"] += diag.get("kept", 0)
        if diag.get("capped"):
            slot["capped"] += 1
    return raws, {"summary": summary, "diagnostics": diagnostics}


def linkedin(profile: dict) -> list[dict]:
    """Page de recherche publique LinkedIn (matrice métier × zone, n8n)."""
    raws, _ = scrape_matrix(profile, ["linkedin"])
    return _normalize_all(raws, "linkedin")


def indeed(profile: dict) -> list[dict]:
    """Page de recherche Indeed FR (matrice métier × zone, n8n)."""
    raws, _ = scrape_matrix(profile, ["indeed"])
    return _normalize_all(raws, "indeed")


def hellowork(profile: dict) -> list[dict]:
    """Page de recherche HelloWork (matrice métier × zone, n8n)."""
    raws, _ = scrape_matrix(profile, ["hellowork"])
    return _normalize_all(raws, "hellowork")


def adzuna_html(profile: dict) -> list[dict]:
    """Page de recherche HTML Adzuna (matrice métier × zone, n8n)."""
    raws, _ = scrape_matrix(profile, ["adzuna"])
    return _normalize_all(raws, "adzuna")


def francetravail_html(profile: dict) -> list[dict]:
    """Page de recherche France Travail (matrice métier × zone, n8n)."""
    raws, _ = scrape_matrix(profile, ["france_travail"])
    return _normalize_all(raws, "france_travail")


def apec(profile: dict) -> list[dict]:
    """Webservice de la page recherche APEC (POST JSON, comme le site)."""
    from .scrapers import metiers_for  # import différé (cycle)
    q = " ".join(metiers_for(profile))
    if not q.strip():
        raise FairUseError("aucun métier cible dans le profil pour interroger l'APEC")
    payload = {"motsCles": [q], "pagination": {"startIndex": 0, "range": 20}}
    text = fetch_page(
        APEC_SEARCH_ENDPOINT, method="POST", json_body=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Referer": "https://www.apec.fr/candidat/recherche-emploi.html/emploi"})
    return _normalize_all(parse_apec_json(json.loads(text)), "apec")


def wttj(profile: dict) -> list[dict]:
    """Welcome to the Jungle via le sitemap officiel.

    Le robots.txt interdit ``*/jobs?query=*`` (page de recherche avec query
    string) : on respecte cette interdiction et on utilise le sitemap
    explicitement fourni par le site, filtré localement par mots-clés.
    """
    from .scrapers import metiers_for  # import différé (cycle)
    ttl_sitemap = _env_int("JOBWATCH_WEB_SITEMAP_TTL", 43200)
    index = fetch_page(WTTJ_SITEMAP_INDEX, ttl=ttl_sitemap)
    shards = [loc for loc in re.findall(r"<loc>([^<]+)</loc>", index)
              if "job-listings" in loc]
    max_shards = max(1, min(_env_int("JOBWATCH_WTTJ_SITEMAP_SHARDS", 1), len(shards) or 1, 3))
    items: list[dict] = []
    for shard in shards[:max_shards]:
        xml_text = fetch_page(shard, ttl=ttl_sitemap)
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml_text):
            item = parse_wttj_sitemap_url(loc)
            if item:
                items.append(item)
    items = wttj_keyword_filter(items, metiers_for(profile))
    return _normalize_all(items, "wttj")


