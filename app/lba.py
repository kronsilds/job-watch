"""La bonne alternance : offres d'apprentissage et de professionnalisation.

API publique de l'État (gratuite, usage non lucratif), une clé par compte :
https://api.apprentissage.beta.gouv.fr (« Se connecter / S'inscrire » →
profil → clé d'API), à placer dans ``LBA_API_KEY``. Sans clé, le connecteur
ne fait aucun appel réseau et ne renvoie rien (comme France Travail/Adzuna).

* Route : ``GET /api/job/v1/search`` (``Authorization: Bearer <clé>``) ;
  filtres ``departements`` (déduits des zones du profil) et ``romes`` ;
  réponse ``{jobs, recruiters, warnings}``. Seules les offres ``jobs``
  (statut ``Active``) sont reprises ; les « recruteurs » (candidatures
  spontanées) ne sont pas des offres.
* Codes ROME : ceux du profil (``data["romes"]``), sinon trouvés depuis les
  métiers via la route publique du site La bonne alternance
  (``GET /api/rome?title=…``, sans clé, cache 7 jours) : premier groupe de
  métiers proposé pour chaque intitulé. Avec des codes ROME, l'API cible le
  métier et la notation trie ; sans (recherche indisponible), l'API renvoie
  les offres récentes tous métiers confondus, filtrées alors par métier.
* La bonne alternance relaie aussi des partenaires (France Travail,
  HelloWork…) : une même offre peut revenir deux fois, elle est dédoublonnée.
"""

from __future__ import annotations

import html
import json
import os
import re
from urllib.parse import quote_plus

import httpx

LBA_BASE = os.getenv("LBA_BASE_URL", "https://api.apprentissage.beta.gouv.fr")
SEARCH_PATH = "/api/job/v1/search"
ROME_LOOKUP = os.getenv("LBA_ROME_URL",
                        "https://labonnealternance.apprentissage.beta.gouv.fr/api/rome")
MAX_ROMES = 20
_REMOTE = {"onsite": "sur site", "remote": "remote", "hybrid": "hybride"}


def api_key() -> str:
    return (os.getenv("LBA_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(api_key())


def _departement_of(ft_code: str) -> list[str]:
    """Départements d'une zone résolue (code France Travail 75D / 11R / INSEE)."""
    code = str(ft_code or "").upper()
    if code.endswith("D"):
        return [code[:-1]]
    if code.endswith("R"):
        from . import websearch
        rows = websearch._fetch_geo_json(
            f"{websearch.GEO_API_BASE}/regions/{code[:-1]}/departements?fields=code")
        return [str(r.get("code")) for r in rows if r.get("code")]
    if re.fullmatch(r"[0-9AB]{5}", code):  # commune INSEE
        return [code[:3]] if code.startswith("97") else [code[:2]]
    return []


def departements_for(profile: dict) -> list[str]:
    from . import websearch
    zones, _warnings = websearch.resolve_zones(profile)
    out: list[str] = []
    for zone in zones:
        for dep in _departement_of(zone.get("ft") or ""):
            if dep not in out:
                out.append(dep)
    return out


def romes_for(profile: dict) -> tuple[list[str], str]:
    """Codes ROME de la recherche et leur origine (``profil`` / ``métiers`` / '')."""
    from . import websearch
    from .scrapers import metiers_for
    own = [str(r).strip().upper() for r in (profile.get("romes") or []) if str(r).strip()]
    if own:
        return own[:MAX_ROMES], "profil"
    found: list[str] = []
    for metier in metiers_for(profile):
        try:
            payload = json.loads(websearch.fetch_page(
                f"{ROME_LOOKUP}?title={quote_plus(metier)}", ttl=7 * 86400,
                headers={"Accept": "application/json"}))
        except Exception:  # recherche indisponible : repli sur le filtre par métier
            continue
        groups = (payload or {}).get("labelsAndRomes") or []
        for code in (groups[0].get("romes") or []) if groups else []:
            if re.fullmatch(r"[A-Z]\d{4}", str(code)) and code not in found:
                found.append(code)
    return found[:MAX_ROMES], ("métiers" if found else "")


def _txt(value) -> str:
    return html.unescape(str(value or "")).strip()


def _city(address: str) -> str:
    """« 20 AVENUE DE SEGUR 75007 PARIS » → « Paris (75007) »."""
    m = re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ' -]+)$", str(address or "").strip())
    if not m:
        return str(address or "").strip()
    return f"{m.group(2).strip().title()} ({m.group(1)})"


def parse_jobs(payload: dict) -> list[dict]:
    """Offres ``jobs`` de la réponse → enregistrements bruts normalisables."""
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for job in (payload or {}).get("jobs") or []:
        offer = job.get("offer") or {}
        if str(offer.get("status") or "Active") != "Active":
            continue
        title = _txt(offer.get("title"))
        if not title:
            continue
        workplace = job.get("workplace") or {}
        contract = job.get("contract") or {}
        ident = job.get("identifier") or {}
        publication = offer.get("publication") or {}
        skills = [str(x) for x in (offer.get("desired_skills") or []) if str(x).strip()]
        acquired = [str(x) for x in (offer.get("to_be_acquired_skills") or []) if str(x).strip()]
        description = _txt(offer.get("description"))
        if acquired:
            description += "\nCompétences visées : " + " ; ".join(acquired)
        diploma = (offer.get("target_diploma") or {}).get("label") or ""
        if diploma:
            description += f"\nDiplôme préparé : {diploma}"
        kinds = [str(k).lower() for k in (contract.get("type") or [])]
        company = _txt(workplace.get("brand") or workplace.get("name")
                       or workplace.get("legal_name")) or "Inconnue"
        location = _city((workplace.get("location") or {}).get("address") or "")
        # Même offre relayée par deux partenaires : une seule fois.
        key = (re.sub(r"\W+", " ", title.lower()).strip(), company.lower(), location.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": f"lba-{ident.get('partner_label') or 'lba'}-{ident.get('id') or ident.get('partner_job_id')}",
            "title": title,
            "company": company,
            "location": location,
            "remote": _REMOTE.get(str(contract.get("remote") or ""), ""),
            # Apprentissage et professionnalisation sont deux formes d'alternance.
            "contract": "alternance" + (f" ({', '.join(kinds)})" if kinds else ""),
            "url": str((job.get("apply") or {}).get("url") or ""),
            "description": description.strip(),
            "skills": skills,
            "published_at": str(publication.get("creation") or ""),
            "expires_at": str(publication.get("expiration") or ""),
        })
    return out


def _get(params: list[tuple[str, str]]) -> dict:
    """Appel HTTP de l'API (isolé pour les tests) ; paramètres répétables."""
    r = httpx.get(LBA_BASE.rstrip("/") + SEARCH_PATH, params=params,
                  headers={"Authorization": f"Bearer {api_key()}",
                           "Accept": "application/json"}, timeout=30)
    if r.status_code == 401:
        raise RuntimeError("La bonne alternance : clé d'API refusée (LBA_API_KEY)")
    r.raise_for_status()
    return r.json()


def search(profile: dict) -> list[dict]:
    """Connecteur ``labonnealternance`` : offres d'alternance de la zone du profil."""
    from .scrapers import metiers_for, normalize
    from .websearch import matches_metiers
    if not enabled():
        return []
    params: list[tuple[str, str]] = []
    for dep in departements_for(profile):
        params.append(("departements", dep))
    romes, _origin = romes_for(profile)
    if romes:
        params.append(("romes", ",".join(romes)))
    payload = _get(params)
    items = parse_jobs(payload)
    if not romes:
        # Sans codes ROME, l'API renvoie tous les métiers : filtre par intitulé.
        items = [i for i in items if matches_metiers(i["title"], metiers_for(profile))]
    cap = max(1, int(os.getenv("JOBWATCH_WEB_MAX_PER_SOURCE", "50") or 50))
    out = []
    for item in items[:cap]:
        rec = normalize(item, "labonnealternance")
        rec["_live"] = True  # statut « Active » sur la plateforme : offre ouverte
        rec["external_id"] = item["id"]  # identifiant stable de l'API
        out.append(rec)
    return out
