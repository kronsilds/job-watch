"""Injection manuelle d'une annonce repérée par l'utilisateur (lien collé).

L'utilisateur trouve une offre par ses propres moyens (recherche manuelle,
newsletter, réseau, site d'entreprise) et colle son **lien**. La plateforme
enchaîne alors exactement le même pipeline que la veille :

1. **récupération** de la page (un seul GET),
2. **extraction** des champs factuels présents dans la page,
3. **pré-notation** heuristique gratuite (passe 1, ``scoring.score_job``),
4. **récupération/complétion** des champs manquants (heuristique locale puis
   LLM light si un modèle est configuré — :mod:`app.offer_enrichment`),
5. **notation finale** pondérée (:func:`app.scoring.score_all`) et ajout à la
   liste (:func:`app.sources.upsert_jobs`).

Conformité — identique à :mod:`app.websearch` pour la veille : User-Agent
honnête, robots.txt consulté (politique ``JOBWATCH_WEB_ROBOTS_POLICY``), quota
quotidien et cache disque partagés avec la veille, aucune protection anti-bot
contournée. Une page illisible (connexion requise, anti-bot) n'est jamais
devinée : elle échoue avec un message explicite, et l'utilisateur peut fournir
l'intitulé/l'entreprise en repli (champs ``title``/``company``).

Aucune donnée n'est inventée : l'extraction ne retient que ce qui figure dans la
page (JSON-LD ``schema.org/JobPosting``, Open Graph, balises méta, ``<h1>``) ;
la passe de complétion ne remplit un trou que si l'annonce le dit elle-même.
"""

from __future__ import annotations

import hashlib
import html as html_module
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from .offer_enrichment import _TIME_MODALITIES, enrich_offer, missing_fields
from .scrapers import enrich_skills_from_text
from . import websearch

# Longueur maximale de description conservée (une annonce reste lisible).
MAX_DESCRIPTION = 20000

# Paramètres de campagne/tracking retirés de la clé d'unicité (l'URL stockée,
# elle, reste intacte pour que le lien « Ouvrir l'annonce » fonctionne).
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "fbclid", "gclid", "gclsrc",
    "mc_cid", "mc_eid", "trk", "trackingid", "refid", "referrer",
}

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_JSONLD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"([a-zA-Z:_-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)

# Types de contrat schema.org traduits vers le lexique du projet
# (``app/data/keywords.json``). ``FULL_TIME`` n'est **pas** traduit : c'est une
# modalité de temps de travail, pas un type de contrat (cf. _TIME_MODALITIES).
_EMPLOYMENT_TYPES = {
    "part_time": "temps partiel",
    "parttime": "temps partiel",
    "contractor": "freelance",
    "temporary": "intérim",
    "intern": "stage",
    "internship": "stage",
    "apprenticeship": "apprentissage",
    "apprentice": "apprentissage",
}
# Unités de rémunération convertibles en annuel ; le reste (heure, jour) est
# ignoré plutôt que d'inventer une base horaire.
_SALARY_UNIT_MULTIPLIER = {"year": 1, "years": 1, "annual": 1, "annum": 1,
                           "month": 12, "months": 12}


# ---------------------------------------------------------------------------
# Lien collé : validation, normalisation, provenance
# ---------------------------------------------------------------------------

def extract_url(text: str) -> str:
    """Extrait la première URL http(s) d'un texte collé.

    Tolère un copier-coller bavard (« Voir l'offre : https://… ») et la
    ponctuation finale capturée par erreur.
    """
    match = _URL_RE.search(str(text or ""))
    return match.group(0).rstrip(".,;:)]}\u00bb") if match else ""


def _validate_url(url: str) -> str:
    """Vérifie qu'il s'agit d'une adresse web http(s) complète."""
    parts = urlparse(url or "")
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError(
            "Lien invalide : collez une adresse web complète (http:// ou https://).")
    return url


def canonical_url(url: str) -> str:
    """URL sans paramètres de tracking : sert de clé d'unicité de l'annonce."""
    parts = urlparse(url or "")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), path, "",
                       urlencode(query), ""))


def source_label(url: str) -> str:
    """Provenance affichée pour une annonce collée (« linkedin.com »)."""
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def external_id_for(url: str) -> str:
    """Identifiant stable d'une annonce collée (re-coller le lien = mise à jour)."""
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:12]
    return f"url-{digest}"


# ---------------------------------------------------------------------------
# Lecture HTML minimale (sans dépendance : regex + unescape)
# ---------------------------------------------------------------------------

def _html_to_text(value: str) -> str:
    """Texte lisible depuis un fragment HTML (script/style retirés, sauts conservés)."""
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|li|div|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    lines = [re.sub(r"[ \t\u00a0\u202f]+", " ", line).strip()
             for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _tag_attrs(tag: str) -> dict[str, str]:
    """Attributs d'une balise (minuscules) depuis sa chaîne brute."""
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        value = match.group(2) if match.group(2) is not None else (
            match.group(3) if match.group(3) is not None else match.group(4))
        out[match.group(1).lower()] = value or ""
    return out


def meta_content(html: str, *keys: str) -> str:
    """Contenu d'une balise ``<meta>`` par ``property`` ou ``name``."""
    wanted = {k.lower() for k in keys}
    for tag in _META_TAG_RE.findall(html or ""):
        attrs = _tag_attrs(tag)
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name in wanted and attrs.get("content", "").strip():
            return _html_to_text(attrs["content"])
    return ""


def _first_match(pattern: re.Pattern, html: str) -> str:
    match = pattern.search(html or "")
    return _html_to_text(match.group(1)) if match else ""


def clean_title(value: str, site: str = "") -> str:
    """Retire le nom du site accolé à l'intitulé (« Data Analyst | LinkedIn »).

    Le retrait n'a lieu que si le dernier segment désigne bien le site
    (``www.linkedin.com`` → « linkedin ») ; sans site connu, le titre est
    conservé tel quel. Un tiret simple n'est jamais un séparateur : il peut
    faire partie du titre (« Chef de projet - Data »).
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or not site:
        return text
    site_low = site.lower()
    label = site_low.split(".")[0]
    for separator in ("|", "·", "—", "–"):
        head, sep, tail = text.rpartition(separator)
        tail = tail.strip()
        if not sep or not head.strip() or len(head.strip()) < 4 or not tail:
            continue
        token = tail.lower().split()[0]
        if token == label or token in site_low or label in token:
            return head.strip()
    return text


# ---------------------------------------------------------------------------
# JSON-LD schema.org/JobPosting : la source la plus fiable des annonces
# ---------------------------------------------------------------------------

def _json_nodes(payload) -> list[dict]:
    """Aplatit une charge JSON-LD (listes et ``@graph`` imbriqués)."""
    out: list[dict] = []
    if isinstance(payload, list):
        for item in payload:
            out += _json_nodes(item)
    elif isinstance(payload, dict):
        out.append(payload)
        for item in payload.get("@graph") or []:
            out += _json_nodes(item)
    return out


def _node_types(node: dict) -> list[str]:
    types = node.get("@type") or node.get("type") or []
    if isinstance(types, str):
        types = [types]
    return [str(x).lower() for x in types]


def _named(value) -> str:
    """Nom lisible d'un nœud schema.org (``{"name": …}``) ou d'une chaîne."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "legalName", "displayName"):
            if str(value.get(key) or "").strip():
                return str(value[key]).strip()
    return ""


def _location_text(value) -> str:
    """Localisation lisible depuis ``jobLocation`` (chaîne, PostalAddress, liste)."""
    for item in (value if isinstance(value, list) else [value]):
        if isinstance(item, str) and item.strip():
            return item.strip()
        if not isinstance(item, dict):
            continue
        address = item.get("address") or item
        if isinstance(address, str) and address.strip():
            return address.strip()
        if isinstance(address, dict):
            city = _named(address.get("addressLocality")) or str(
                address.get("addressLocality") or "").strip()
            country = _named(address.get("addressCountry")) or str(
                address.get("addressCountry") or "").strip()
            if city and country:
                return f"{city} ({country})"
            if city:
                return city
    return ""


def _contract_text(value) -> str:
    """Type de contrat depuis ``employmentType`` (schema.org).

    Les valeurs normalisées sont traduites vers le lexique du projet ; une valeur
    libre déjà exploitable (« CDI », « Stage ») est conservée telle quelle.
    """
    from .heuristics import detect_contracts  # import local (module léger)
    for item in (value if isinstance(value, list) else [value]):
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower().replace("-", "_").replace(" ", "_")
        if key in _EMPLOYMENT_TYPES:
            return _EMPLOYMENT_TYPES[key]
        found = [c for c in detect_contracts(text)
                 if c.lower() not in _TIME_MODALITIES]
        if found:
            return found[0].lower()
    return ""


def _remote_text(node: dict) -> str:
    """Modalité de télétravail déclarée par l'annonce (``jobLocationType``)."""
    raw = str(node.get("jobLocationType") or "").lower()
    if not raw:
        return ""
    if "telecommute" in raw or "remote" in raw or "home" in raw:
        return "remote"
    return ""


def _salary_range(node) -> tuple[int | None, int | None]:
    """Fourchette annuelle en euros depuis ``baseSalary`` (jamais convertie à l'aveugle)."""
    for item in (node if isinstance(node, list) else [node]):
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or item.get("currencyCode") or "").upper()
        if currency and currency not in ("EUR", "€"):
            continue
        raw_value = item.get("value")
        unit = ""
        if isinstance(raw_value, dict):
            unit = str(raw_value.get("unitText") or raw_value.get("unitCode") or "").lower()
            candidates = [raw_value.get(k) for k in ("value", "minValue", "maxValue")]
        else:
            candidates = [raw_value]
        if unit.startswith("hour") or unit.startswith("day"):
            continue  # base horaire/jour : non convertible sans inventer un volume
        multiplier = next((v for k, v in _SALARY_UNIT_MULTIPLIER.items()
                           if unit.startswith(k)), 1)
        values = []
        for candidate in candidates:
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                amount = int(round(float(candidate) * multiplier))
                if 10000 <= amount <= 1000000:
                    values.append(amount)
        if values:
            return min(values), max(values)
    return None, None


def jsonld_job_postings(html: str) -> list[dict]:
    """Offres ``schema.org/JobPosting`` déclarées dans les blocs JSON-LD de la page."""
    import json as _json
    found: list[dict] = []
    for block in _JSONLD_RE.findall(html or ""):
        raw = block.strip()
        if not raw:
            continue
        try:
            payload = _json.loads(raw)
        except (ValueError, TypeError):
            continue  # bloc JSON-LD illisible : ignoré, jamais deviné
        for node in _json_nodes(payload):
            if "jobposting" in _node_types(node):
                found.append(node)
    return found


def _job_posting_offer(node: dict, page_url: str = "") -> dict:
    """Champs d'annonce factuels extraits d'un nœud ``JobPosting``."""
    salary_min, salary_max = _salary_range(node.get("baseSalary"))
    return {
        "title": _html_to_text(str(node.get("title") or node.get("name") or "")),
        "company": _named(node.get("hiringOrganization")),
        "location": _location_text(node.get("jobLocation")),
        "remote": _remote_text(node),
        "contract": _contract_text(node.get("employmentType")),
        "salary_min": salary_min,
        "salary_max": salary_max,
        "url": str(node.get("url") or "").strip() or page_url,
        "description": _html_to_text(str(node.get("description") or "")),
    }


# ---------------------------------------------------------------------------
# Analyse de la page : JSON-LD d'abord, puis replis Open Graph / HTML
# ---------------------------------------------------------------------------

def parse_offer_page(html: str, url: str = "") -> tuple[dict, dict]:
    """Extrait une annonce factuelle d'une page HTML déjà téléchargée.

    Ordre de priorité : JSON-LD ``JobPosting`` (le plus complet et le plus sûr),
    puis Open Graph / ``<title>`` / ``<h1>`` / méta description. Aucun champ
    n'est écrasé par un repli moins fiable. Le second élément retourné est le
    compte rendu d'extraction (provenance, champs trouvés, avertissements).
    """
    site = source_label(url) if url else ""
    offer: dict = {}
    extraction: list[str] = []
    for node in jsonld_job_postings(html):
        candidate = _job_posting_offer(node, url)
        if candidate.get("title") or candidate.get("description"):
            offer = candidate
            extraction.append("JSON-LD JobPosting")
            break

    if not str(offer.get("title") or "").strip():
        candidates = (
            ("og:title", meta_content(html, "og:title", "twitter:title")),
            ("<title>", _first_match(_TITLE_RE, html)),
            ("<h1>", _first_match(_H1_RE, html)),
        )
        for source, raw in candidates:
            title = clean_title(raw, site)
            if title:
                offer["title"] = title
                extraction.append(source)
                break

    # ``og:site_name`` est le nom du site, pas celui de l'employeur : il n'est
    # jamais promu en entreprise (aucune donnée inventée, aucune confusion).
    if not str(offer.get("description") or "").strip():
        description = meta_content(html, "og:description", "description",
                                   "twitter:description")
        if description:
            offer["description"] = description
            extraction.append("meta description")

    offer.setdefault("title", "")
    offer.setdefault("company", "")
    offer.setdefault("location", "")
    offer.setdefault("remote", "")
    offer.setdefault("contract", "")
    offer.setdefault("skills", [])
    offer.setdefault("sectors", [])
    offer.setdefault("salary_min", None)
    offer.setdefault("salary_max", None)
    offer["url"] = str(offer.get("url") or "").strip() or url

    warnings: list[str] = []
    if not extraction:
        warnings.append("Aucune donnée d'annonce détectée dans la page "
                        "(connexion requise ou page de recherche ?).")
    return offer, {"source": site, "extraction": extraction, "warnings": warnings}


def build_offer(url: str, html: str, overrides: dict | None = None,
                provider=None) -> tuple[dict, dict]:
    """Transforme une page téléchargée en offre prête pour le pipeline.

    Enchaîne : extraction → replis fournis par l'utilisateur (uniquement sur les
    champs vides) → compétences/secteurs depuis le texte → complétion des champs
    manquants (heuristique locale, puis LLM light si un modèle est configuré).
    """
    offer, meta = parse_offer_page(html, url)
    applied: list[str] = []
    for key in ("title", "company", "description"):
        value = str((overrides or {}).get(key) or "").strip()
        if value and not str(offer.get(key) or "").strip():
            offer[key] = value
            applied.append(key)
    if applied:
        meta["extraction"].append("repli fourni : " + ", ".join(applied))
    offer = enrich_skills_from_text(offer)
    offer = enrich_offer(offer, provider=provider)
    description = str(offer.get("description") or "")
    if len(description) > MAX_DESCRIPTION:
        offer["description"] = description[:MAX_DESCRIPTION].rstrip() + "…"
    meta["missing"] = missing_fields(offer)
    return offer, meta


def fetch_offer(url: str, overrides: dict | None = None,
                provider=None) -> tuple[dict, dict]:
    """Récupère la page du lien collé (charte fair use) et retourne l'offre extraite."""
    link = extract_url(url) or str(url or "").strip()
    _validate_url(link)
    try:
        html = websearch.fetch_page(link)
    except websearch.FairUseError:
        raise
    except httpx.HTTPStatusError as exc:
        raise websearch.FairUseError(
            f"page indisponible (HTTP {exc.response.status_code})") from exc
    except httpx.HTTPError as exc:
        raise websearch.FairUseError(
            f"erreur réseau vers {urlparse(link).netloc}: {type(exc).__name__}") from exc
    offer, meta = build_offer(link, html, overrides, provider)
    meta["url"] = link
    return offer, meta


# ---------------------------------------------------------------------------
# Pipeline complet : pré-notation → récupération → notation → ajout à la liste
# ---------------------------------------------------------------------------

def store_offer(db, offer: dict, meta: dict,
                profile_data: dict | None = None, user_id=None) -> dict:
    """Pré-notation → enregistrement (complétion comprise) → note finale.

    Étape terminale du pipeline, séparée de la récupération réseau pour que les
    endpoints streamés puissent émettre leur progression entre les deux. Retourne
    ``{"job": …, "report": …}`` (compte rendu : provenance, pré-note, note finale,
    champs manquants, avertissements).
    """
    from .applications import job_payload
    from .models import Event, Job
    from .scoring import score_all
    from .sources import prescore_and_select, upsert_jobs
    from .user_jobs import overlay_job
    title = str(offer.get("title") or "").strip()
    if not title:
        raise ValueError(
            "Intitulé du poste introuvable dans la page (connexion requise, "
            "anti-bot ou page de recherche) : collez le lien d'une annonce "
            "précise ou renseignez l'intitulé en repli.")
    url = str((meta or {}).get("url") or offer.get("url") or "")
    external_id = external_id_for(url)
    offer["external_id"] = external_id
    offer["_source"] = str((meta or {}).get("source") or "lien")
    prescore = None
    if profile_data:
        _selected, prescored = prescore_and_select([offer], profile_data, top_n=1)
        prescore = prescored[0].get("_prescore") if prescored else None
    created = db.query(Job).filter_by(external_id=external_id).first() is None
    # upsert_jobs déroule la passe 1 (pré-note) puis la passe 2 ciblée
    # (complétion heuristique + LLM light des champs manquants).
    upsert_jobs(db, [offer], profile_data=profile_data, top_n=1)
    job = db.query(Job).filter_by(external_id=external_id).first()
    if job is None:
        raise ValueError("Import impossible : l'annonce n'a pas pu être enregistrée.")
    if profile_data:
        score_all(db, profile_data, user_id)
    db.add(Event(user_id=user_id,kind="import",
                 message=f"Annonce ajoutée par lien : {job.title}",
                 metadata_json={"url": url, "job_id": job.id,
                                "source": meta.get("source"), "prescore": prescore,
                                "score": job.score}))
    db.commit()
    rating = None
    if user_id is not None:
        from .models import JobRating
        rating = db.query(JobRating).filter_by(user_id=user_id, job_id=job.id).first()
    score, confidence, decision, explanation = overlay_job(job, rating)
    report = {
        "url": url,
        "canonical_url": canonical_url(url),
        "source": meta.get("source") or "",
        "extraction": list(meta.get("extraction") or []),
        "warnings": list(meta.get("warnings") or []),
        "created": created,
        "scored": bool(profile_data),
        "prescore": round(float(prescore), 1) if isinstance(prescore, (int, float)) else None,
        "score": score,
        "confidence": confidence,
        "decision": decision,
        "matched_skills": explanation.get("matched_offer_skills") or [],
        "gaps": explanation.get("gaps") or [],
        "missing_fields": meta.get("missing") or [],
    }
    return {"job": job_payload(job), "report": report}


def import_from_url(db, url: str, profile_data: dict | None = None,
                    overrides: dict | None = None, provider=None, user_id=None) -> dict:
    """Ajoute à la liste une annonce repérée par l'utilisateur (lien collé).

    Déroule sur cette unique annonce exactement le pipeline de la veille :
    récupération de la page, extraction factuelle, pré-notation heuristique
    (passe 1, gratuite), complétion des champs manquants (heuristique puis LLM
    light), note finale pondérée (passe 2) et journalisation d'un événement
    ``import``. Re-coller le même lien met à jour la même offre (clé d'unicité
    dérivée de l'URL), sans doublon.
    """
    offer, meta = fetch_offer(url, overrides=overrides, provider=provider)
    return store_offer(db, offer, meta, profile_data, user_id)