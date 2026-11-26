"""Pages carrières d'entreprises cibles (connecteur ``career_pages``).

Le profil liste les pages carrières à suivre (``data["career_pages"]``) :
l'offre publiée directement par l'employeur arrive souvent avant (ou sans)
les agrégateurs. Selon l'hébergeur de la page, on lit le flux public prévu
pour les intégrations :

=================  ==========================================================
Greenhouse         ``boards-api.greenhouse.io/v1/boards/{entreprise}/jobs``
Lever              ``api.lever.co/v0/postings/{entreprise}?mode=json``
Ashby              ``api.ashbyhq.com/posting-api/job-board/{entreprise}``
SmartRecruiters    ``api.smartrecruiters.com/v1/companies/{entreprise}/postings``
Teamtailor         flux RSS ``{site}/jobs.rss``
autre site         données ``schema.org/JobPosting`` (JSON-LD) de la page
=================  ==========================================================

Chaque page = une requête via :func:`app.websearch.fetch_page` (charte fair
use : robots.txt, quota quotidien, cache, délai). Les offres sont filtrées
par métier (racines des mots) et par pays, et classées zone d'abord. Une offre
affichée sur la page carrières est ouverte : elle est marquée « confirmée en
ligne » et présentée quel que soit son âge (voir :func:`app.freshness.freshness`).
"""

from __future__ import annotations

import html as _html
import json
import re
from urllib.parse import urlparse

from . import websearch


def _clean(text) -> str:
    """HTML (éventuellement échappé, cas Greenhouse) → texte simple."""
    raw = _html.unescape(str(text or ""))
    return websearch._html_to_text(raw)


def detect(url: str) -> tuple[str, str]:
    """``(plateforme, identifiant)`` d'une page carrières, ou ``("page", url)``."""
    u = urlparse(url.strip())
    host = (u.netloc or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if "greenhouse.io" in host:
        if host.startswith("boards-api.") and len(parts) >= 3:
            return "greenhouse", parts[2]
        if parts:
            return "greenhouse", parts[0]
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return ("lever_eu" if ".eu." in host else "lever"), parts[0]
    if host == "jobs.ashbyhq.com" and parts:
        return "ashby", parts[0]
    if host in ("careers.smartrecruiters.com", "jobs.smartrecruiters.com") and parts:
        return "smartrecruiters", parts[0]
    if host.endswith(".teamtailor.com"):
        return "teamtailor", f"{u.scheme or 'https'}://{host}"
    return "page", url.strip()


# ---------------------------------------------------------------------------
# Parseurs purs (réponse → enregistrements bruts), testables sans réseau
# ---------------------------------------------------------------------------

def parse_greenhouse(payload: dict) -> list[dict]:
    out = []
    for job in (payload or {}).get("jobs") or []:
        meta = {str(m.get("name") or ""): m.get("value") for m in job.get("metadata") or []
                if isinstance(m, dict)}
        out.append({
            "title": str(job.get("title") or "").strip(),
            "company": str(job.get("company_name") or "").strip(),
            "location": str((job.get("location") or {}).get("name") or ""),
            "contract": str(meta.get("Employment Type") or meta.get("Contract Type") or ""),
            "url": str(job.get("absolute_url") or ""),
            "description": _clean(job.get("content")),
            "published_at": str(job.get("first_published") or job.get("updated_at") or ""),
            "expires_at": str(job.get("application_deadline") or ""),
        })
    return out


def parse_lever(payload: list, company: str = "") -> list[dict]:
    out = []
    for job in payload if isinstance(payload, list) else []:
        cats = job.get("categories") or {}
        out.append({
            "title": str(job.get("text") or "").strip(),
            "company": company,
            "location": str(cats.get("location") or ""),
            "contract": str(cats.get("commitment") or ""),
            "remote": {"remote": "remote", "hybrid": "hybride", "onsite": "sur site"}.get(
                str(job.get("workplaceType") or "").lower(), ""),
            "url": str(job.get("hostedUrl") or ""),
            "description": str(job.get("descriptionPlain") or "").strip()
            or _clean(job.get("description")),
            "published_at": str(job.get("createdAt") or ""),
        })
    return out


def parse_ashby(payload: dict, company: str = "") -> list[dict]:
    out = []
    for job in (payload or {}).get("jobs") or []:
        if job.get("isListed") is False:
            continue
        workplace = str(job.get("workplaceType") or "").lower()
        out.append({
            "title": str(job.get("title") or "").strip(),
            "company": company,
            "location": str(job.get("location") or ""),
            "contract": str(job.get("employmentType") or ""),
            "remote": "remote" if job.get("isRemote") and workplace != "hybrid"
            else ("hybride" if workplace == "hybrid" else ""),
            "url": str(job.get("jobUrl") or ""),
            "description": str(job.get("descriptionPlain") or "").strip()
            or _clean(job.get("descriptionHtml")),
            "published_at": str(job.get("publishedAt") or ""),
        })
    return out


def parse_smartrecruiters(payload: dict, company: str = "") -> list[dict]:
    out = []
    for job in (payload or {}).get("content") or []:
        loc = job.get("location") or {}
        level = (job.get("experienceLevel") or {}).get("id") or ""
        out.append({
            "title": str(job.get("name") or "").strip(),
            "company": str((job.get("company") or {}).get("name") or company),
            "location": ", ".join(str(x) for x in (loc.get("city"), loc.get("region")) if x),
            # « internship » est un niveau d'expérience chez SmartRecruiters.
            "contract": "stage" if level == "internship"
            else str((job.get("typeOfEmployment") or {}).get("label") or ""),
            "remote": "remote" if loc.get("remote") else ("hybride" if loc.get("hybrid") else ""),
            "url": f"https://jobs.smartrecruiters.com/{company}/{job.get('id')}" if job.get("id") else "",
            "description": "",
            "published_at": str(job.get("releasedDate") or ""),
        })
    return out


def parse_teamtailor_rss(xml_text: str) -> list[dict]:
    channel = re.search(r"<channel>([\s\S]*?)<item>", xml_text or "")
    company = ""
    if channel:
        m = re.search(r"<title>([^<]*)</title>", channel.group(1))
        company = _html.unescape(m.group(1)).strip() if m else ""
    out = []
    for item in re.findall(r"<item>([\s\S]*?)</item>", xml_text or ""):
        def tag(name):
            m = re.search(rf"<{name}>([\s\S]*?)</{name}>", item)
            return m.group(1).strip() if m else ""
        cities = [c.strip() for c in re.findall(r"<tt:city>([^<]*)</tt:city>", item) if c.strip()]
        desc = tag("description")
        desc = re.sub(r"^<!\[CDATA\[|\]\]>$", "", desc)
        remote = tag("remoteStatus").lower()
        out.append({
            "title": _html.unescape(tag("title")),
            "company": company,
            "location": " / ".join(dict.fromkeys(cities)),
            "remote": {"fully": "remote", "hybrid": "hybride", "temporary": "remote"}.get(remote, ""),
            "url": tag("link"),
            "description": _clean(desc),
            "published_at": tag("pubDate"),
        })
    return out


def parse_jsonld_page(html_text: str, page_url: str) -> list[dict]:
    """Offres déclarées en JSON-LD ``JobPosting`` sur une page carrières."""
    from .manual import _job_posting_offer, jsonld_job_postings
    return [_job_posting_offer(node, page_url) for node in jsonld_job_postings(html_text)]


# ---------------------------------------------------------------------------
# Récupération (une requête par page, charte fair use)
# ---------------------------------------------------------------------------

def _pretty(ident: str) -> str:
    """« ma-boite » → « Ma Boite » (nom d'entreprise tiré de l'URL)."""
    return str(ident or "").replace("-", " ").replace("_", " ").strip().title()


def _json(url: str):
    return json.loads(websearch.fetch_page(url, headers={"Accept": "application/json"}))


def fetch_board(url: str) -> list[dict]:
    """Offres brutes d'une page carrières (lève FairUseError en cas d'échec)."""
    kind, ident = detect(url)
    if kind == "greenhouse":
        return parse_greenhouse(_json(
            f"https://boards-api.greenhouse.io/v1/boards/{ident}/jobs?content=true"))
    if kind in ("lever", "lever_eu"):
        api = "api.eu.lever.co" if kind == "lever_eu" else "api.lever.co"
        return parse_lever(_json(f"https://{api}/v0/postings/{ident}?mode=json"), _pretty(ident))
    if kind == "ashby":
        return parse_ashby(_json(f"https://api.ashbyhq.com/posting-api/job-board/{ident}"),
                           _pretty(ident))
    if kind == "smartrecruiters":
        return parse_smartrecruiters(_json(
            f"https://api.smartrecruiters.com/v1/companies/{ident}/postings?limit=100"), ident)
    if kind == "teamtailor":
        return parse_teamtailor_rss(websearch.fetch_page(f"{ident}/jobs.rss"))
    page = websearch.fetch_page(ident)
    offers = parse_jsonld_page(page, ident)
    if not offers:
        raise websearch.FairUseError(
            f"{urlparse(ident).netloc} : aucune offre lisible (pas de données JobPosting) — "
            "collez plutôt le lien d'une offre précise")
    return offers


def career_pages_for(profile: dict) -> list[str]:
    raw = (profile or {}).get("career_pages") or []
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    out: list[str] = []
    for url in raw:
        url = str(url or "").strip()
        if url.startswith("http") and url not in out:
            out.append(url)
    return out


def search(profile: dict) -> list[dict]:
    """Connecteur ``career_pages`` : offres des pages carrières du profil.

    Une page en échec n'empêche pas les autres ; si toutes échouent, l'erreur
    remonte dans le rapport de veille.
    """
    from .scrapers import metiers_for, normalize
    pages = career_pages_for(profile)
    if not pages:
        return []
    items: list[dict] = []
    errors: list[str] = []
    for url in pages:
        try:
            items += [i for i in fetch_board(url) if i.get("title") and i.get("url")]
        except Exception as exc:  # une page en échec ne bloque pas les autres
            errors.append(f"{url} : {exc}")
    if errors and not items:
        raise websearch.FairUseError("; ".join(errors))
    items = websearch.wttj_keyword_filter(items, metiers_for(profile))
    # Une offre affichée sur la page carrières est ouverte, même publiée il y a
    # longtemps : pas de filtre d'âge (sauf date limite dépassée).
    items = websearch.drop_abroad(websearch.drop_expired(items), profile, strict=True)
    items = websearch.rank_by_place_and_date(items, profile)
    cap = max(1, websearch._env_int("JOBWATCH_WEB_MAX_PER_SOURCE", 50))
    return websearch.mark_live([normalize(i, "career_pages") for i in items[:cap]])
