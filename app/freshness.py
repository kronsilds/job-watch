"""Fraîcheur des annonces : date de publication, expiration, offres terminées.

Trois signaux, du moins coûteux au plus coûteux :

1. **Date lue à la source** (gratuit) : ``<time datetime>`` LinkedIn,
   « Publié il y a 12 jours » France Travail, ``datePosted`` / ``validThrough``
   JSON-LD, ``<lastmod>`` du sitemap WTTJ… (:func:`parse_date`).
2. **Dernière apparition en veille** (gratuit) : une offre qui ne ressort plus
   des recherches depuis plus de ``max_age_days`` est considérée périmée.
3. **Vérification de la page** (une requête, quota partagé) : 404/410,
   redirection vers une page de recherche, mention « offre n'est plus
   disponible », ``validThrough`` dépassé (:func:`check_offer`).

:func:`freshness` combine ces signaux pour décider si une offre est encore
présentée par défaut. L'âge maximum se règle par profil
(``data["max_age_days"]``, défaut ``JOBWATCH_MAX_AGE_DAYS`` ou 45 jours).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .contracts import fold

DEFAULT_MAX_AGE_DAYS = 45
# Délai avant de re-vérifier la page d'une offre déjà contrôlée.
RECHECK_DAYS = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value) -> datetime | None:
    """Datetime « aware » UTC (les colonnes SQLite rendent des datetimes naïfs)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def max_age_days(profile: dict | None = None) -> int:
    """Âge maximum d'une offre présentée : profil, sinon env, sinon 45 jours."""
    raw = (profile or {}).get("max_age_days")
    if raw in (None, ""):
        raw = os.getenv("JOBWATCH_MAX_AGE_DAYS") or DEFAULT_MAX_AGE_DAYS
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_DAYS


# ---------------------------------------------------------------------------
# Dates : ISO, jj/mm/aaaa, « 12 novembre 2026 », relatives FR/EN
# ---------------------------------------------------------------------------

_MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
           "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
           "decembre": 12, "january": 1, "february": 2, "march": 3, "april": 4,
           "may": 5, "june": 6, "july": 7, "august": 8, "september": 9,
           "october": 10, "november": 11, "december": 12}
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?")
_DMY = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
_DMY_TEXT = re.compile(r"\b(\d{1,2})(?:er)?\s+([a-z]+)\s+(\d{4})\b")
_RELATIVE = re.compile(
    r"(?:il y a|depuis)\s+(?:plus de\s+)?(\d+|un|une)\s*\+?\s*"
    r"(minutes?|min|heures?|h|jours?|j|semaines?|mois)\b"
    r"|(\d+|a|an|one)\s*\+?\s*(minutes?|hours?|days?|weeks?|months?)\s+ago")


def _unit_days(unit: str) -> int:
    """Nombre de jours d'une unité relative (minutes/heures → 0)."""
    for prefix, days in (("min", 0), ("h", 0), ("j", 1), ("d", 1), ("sem", 7),
                         ("w", 7), ("mois", 30), ("month", 30)):
        if unit.startswith(prefix):
            return days
    return 1


def _num(raw: str) -> int:
    return 1 if raw in ("un", "une", "a", "an", "one") else int(raw)


def _epoch(value) -> datetime | None:
    """Horodatage Unix en secondes ou millisecondes → datetime UTC."""
    try:
        seconds = float(value) / (1000 if float(value) > 1e11 else 1)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_date(value, now: datetime | None = None) -> datetime | None:
    """Date (UTC) d'une valeur de site : ISO, ``jj/mm/aaaa``, texte ou relatif.

    « Publié il y a 26 jours », « aujourd'hui », « hier », « 3 days ago »,
    « 2026-11-20T10:00:00Z », « 20/11/2026 », « 12 novembre 2026 ». Renvoie
    None si rien d'exploitable (jamais deviné).
    """
    if isinstance(value, datetime):
        return aware(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch(value)
    text = str(value or "").strip()
    if not text:
        return None
    now = aware(now) or utcnow()
    if re.fullmatch(r"\d{10}|\d{13}", text):  # horodatage Unix (s ou ms, API Lever)
        return _epoch(int(text))
    if re.match(r"^[A-Za-z]{3}, \d{1,2} [A-Za-z]{3} \d{4}", text):  # RFC 822 (flux RSS)
        from email.utils import parsedate_to_datetime
        try:
            return aware(parsedate_to_datetime(text)).astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
    m = _ISO.search(text)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
            return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
        except ValueError:
            return None
    low = fold(text)
    m = _DMY.search(low)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    m = _DMY_TEXT.search(low)
    if m and m.group(2) in _MONTHS:
        try:
            return datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    if re.search(r"aujourd'hui|\btoday\b|a l'instant|\bjust now\b", low):
        return _midnight(now)
    if re.search(r"\bhier\b|\byesterday\b", low):
        return _midnight(now) - timedelta(days=1)
    m = _RELATIVE.search(low)
    if m:
        raw, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return _midnight(now) - timedelta(days=_num(raw) * _unit_days(unit))
    return None


_PUBLISHED_HINT = re.compile(
    r"(?:publi[ée]e?|mise? en ligne|posted|actualis[ée]e?)\s*(?:le|il y a|depuis|:)?\s*"
    r"[^|<>]{0,40}", re.I)


def find_published(text: str, now: datetime | None = None,
                   require_hint: bool = True) -> datetime | None:
    """Date de publication trouvée dans le texte d'une carte de résultat.

    Avec ``require_hint`` (défaut), exige « Publié… / Posted… » devant la date
    pour ne pas confondre avec « Employeur actif il y a 3 jours ».
    """
    text = str(text or "")
    if require_hint:
        for m in _PUBLISHED_HINT.finditer(text):
            found = parse_date(m.group(0), now)
            if found:
                return found
        return None
    return parse_date(text, now)


def iso(value) -> str | None:
    value = aware(value)
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Décision « présentée par défaut ou non »
# ---------------------------------------------------------------------------

def _get(job, key, default=None):
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def freshness(job, max_age: int = DEFAULT_MAX_AGE_DAYS,
              now: datetime | None = None) -> dict:
    """``{fresh, reason, age_days, basis}`` pour une offre (ORM ou dict).

    * ``availability == "expired"`` (page vérifiée) → terminée ;
    * ``expires_at`` dépassée → date limite dépassée ;
    * ``published_at`` plus vieille que ``max_age`` → ancienne ;
    * sans date de publication : dernière apparition en veille (ou date
      d'import) plus vieille que ``max_age`` → périmée.
    """
    now = aware(now) or utcnow()
    pub = aware(_get(job, "published_at"))
    age = (now - pub).days if pub else None
    out = {"fresh": True, "reason": "", "age_days": age,
           "basis": "publication" if pub else ""}
    if (_get(job, "availability") or "active") == "expired":
        note = _get(job, "availability_note") or "annonce terminée"
        return {**out, "fresh": False, "reason": note, "basis": "verification"}
    exp = aware(_get(job, "expires_at"))
    if exp and exp < now:
        return {**out, "fresh": False, "basis": "expiration",
                "reason": f"date limite de candidature dépassée ({exp:%d/%m/%Y})"}
    if pub:
        if age > max_age:
            return {**out, "fresh": False, "reason": f"publiée il y a {age} jours"}
        return out
    seen = aware(_get(job, "last_seen_at")) or aware(_get(job, "created_at"))
    if seen:
        seen_age = (now - seen).days
        if seen_age > max_age:
            return {**out, "fresh": False, "basis": "veille",
                    "reason": f"plus revue en veille depuis {seen_age} jours"}
        out["basis"] = "veille"
    return out


# ---------------------------------------------------------------------------
# Vérification de la page d'une offre (une requête, charte fair use)
# ---------------------------------------------------------------------------

_EXPIRED = re.compile(
    r"(?:offre|annonce|poste)s? (?:d'emploi )?(?:n'est plus (?:disponible|en ligne|active|"
    r"d'actualite|accessible)|a expire|est expiree|a ete (?:pourvue?|supprimee|retiree|"
    r"desactivee|cloturee|archivee)|est (?:pourvue?|cloturee|fermee|terminee|archivee))"
    # « Cette offre d'emploi chez RATP n'est plus disponible » (HelloWork) :
    # quelques mots tolérés entre « offre » et la mention, dans la même phrase.
    r"|\bcette (?:offre|annonce)\b[^.!?<>]{0,60}?\b(?:n'est plus (?:disponible|en ligne|"
    r"active|accessible)|a expire|a ete pourvue)"
    r"|offre expiree|annonce expiree|cette offre n'existe plus"
    r"|n'accepte plus de candidatures|candidatures (?:sont )?(?:closes|fermees|cloturees)"
    r"|no longer accepting applications|(?:job|position|posting|offer) (?:is )?no longer "
    r"(?:available|active|open)|this job (?:has )?expired|job (?:posting )?has expired"
    r"|position has been filled")
_LISTING_PATH = re.compile(r"(?:recherche|search|resultats?|/jobs/?$|/emplois?/?$|/offres/?$)",
                           re.I)
_INVISIBLE = re.compile(r"<(script|style|template|noscript)\b[^>]*>[\s\S]*?</\1>", re.I)


def page_status(html: str, final_url: str = "", original_url: str = "",
                now: datetime | None = None) -> tuple[str, str]:
    """``("active" | "expired" | "unknown", motif)`` d'après la page de l'offre."""
    from .manual import jsonld_job_postings  # import local (cycle)
    now = aware(now) or utcnow()
    postings = jsonld_job_postings(html or "")
    for node in postings:
        until = parse_date(node.get("validThrough"), now)
        if until and until < now:
            return "expired", f"date limite de candidature dépassée ({until:%d/%m/%Y})"
    visible = fold(re.sub(r"<[^>]+>", " ", _INVISIBLE.sub(" ", html or "")))
    visible = re.sub(r"\s+", " ", visible)  # texte réparti sur plusieurs balises/lignes
    m = _EXPIRED.search(visible)
    if m:
        return "expired", f"la page indique « {m.group(0)} »"
    if final_url and original_url:
        a, b = urlparse(original_url), urlparse(final_url)
        if (a.netloc, a.path.rstrip("/")) != (b.netloc, b.path.rstrip("/")) \
                and _LISTING_PATH.search(b.path):
            return "expired", "l'annonce redirige vers une page de recherche"
    if postings:
        return "active", "annonce en ligne (données JobPosting présentes)"
    return "unknown", "page lue, statut de l'annonce indéterminé"


def check_offer(url: str, now: datetime | None = None) -> dict:
    """Vérifie la page d'une offre : ``{status, reason}``, sans jamais lever.

    Une seule requête via :func:`app.websearch.fetch_page_info` (robots.txt,
    quota quotidien, délai, cache 12 h). 404/410 → terminée ; refus anti-bot
    ou quota atteint → ``unknown`` (rien n'est contourné ni supposé).
    """
    import httpx

    from . import websearch
    if not str(url or "").startswith("http"):
        return {"status": "unknown", "reason": "pas de lien vers l'annonce"}
    try:
        html, final_url = websearch.fetch_page_info(url, ttl=43200)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code in (404, 410):
            return {"status": "expired", "reason": f"la page de l'annonce n'existe plus (HTTP {code})"}
        return {"status": "unknown", "reason": f"erreur HTTP {code}"}
    except websearch.BudgetExceeded as exc:
        return {"status": "unknown", "reason": str(exc), "budget": True}
    except websearch.FairUseError as exc:
        return {"status": "unknown", "reason": str(exc)}
    except Exception as exc:  # pragma: no cover - garde-fou réseau
        return {"status": "unknown", "reason": f"{type(exc).__name__}: {exc}"}
    status, reason = page_status(html, final_url, url, now)
    return {"status": status, "reason": reason}


def apply_check(job, result: dict, now: datetime | None = None) -> None:
    """Reporte le résultat d'une vérification sur l'offre (ORM)."""
    now = aware(now) or utcnow()
    job.availability_checked_at = now
    status = result.get("status")
    if status == "expired":
        job.availability = "expired"
        job.availability_note = str(result.get("reason") or "annonce terminée")[:300]
    elif status == "active":
        job.availability = "active"
        job.availability_note = ""
    else:
        job.availability_note = str(result.get("reason") or "")[:300]


def needs_check(job, now: datetime | None = None) -> bool:
    """Vrai si l'offre n'a pas été vérifiée depuis ``RECHECK_DAYS`` jours."""
    now = aware(now) or utcnow()
    checked = aware(_get(job, "availability_checked_at"))
    return checked is None or (now - checked) > timedelta(days=RECHECK_DAYS)
