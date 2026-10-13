"""Generalist, lexicon-driven heuristics for CV/profile extraction.

These heuristics are intentionally meta-profession: they rely on a broad
multi-domain keyword lexicon (``app/data/keywords.json``) plus a few robust
language patterns (year counts, contacts, remote modalities, city detection).
They never invent facts: they only report what is found in the text.
"""

from __future__ import annotations

import re

from .prompts import load_keywords

_LANG = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .-]*(?:\s[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .-]*){0,4}$")
_SECTION_HEADERS = {
    "cv", "curriculum", "vitae", "experience", "expériences", "formation",
    "competences", "compétences", "profil", "contact", "projets", "langues",
    "diplomes", "diplômes", "parcours", "resume", "skills", "education",
    "hobbies", "centres", "references", "références",
}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _tokenize_norm(text: str) -> str:
    return _norm(text).replace("’", "'")


def detect_name(text: str) -> str:
    """Best-effort, generalist name detection from the top of the CV.

    Looks at the first ~12 short, alphabetical lines and returns the first
    plausible ``Prénom Nom`` (>= 2 capitalized words, no digits).
    """
    lines = [ln.strip() for ln in (text or "").splitlines()[:12] if ln.strip()]
    for line in lines:
        if len(line) > 40 or any(ch.isdigit() for ch in line):
            continue
        if line.lower() in _SECTION_HEADERS:
            continue
        words = line.split()
        if not (2 <= len(words) <= 4):
            continue
        if not _LANG.match(line):
            continue
        caps = sum(1 for w in words if re.match(r"^[A-ZÀ-Ý][a-zà-ÿ]+$", w))
        if caps >= 2:
            return " ".join(w.title() for w in words)
    return ""


def _find_matches(text: str, candidates: list[str]) -> list[tuple[int, str]]:
    low = _tokenize_norm(text)
    found: list[tuple[int, str]] = []
    for cand in candidates:
        pat = re.escape(cand)
        pattern = rf"\b{pat}\b" if cand[0].isalnum() else pat
        for m in re.finditer(pattern, low):
            found.append((m.start(), cand))
    return found


def detect_jobs(text: str, exclude_name: str = "") -> tuple[str, str]:
    """Return (main_job, secondary_job) using the generic title lexicon."""
    titles = load_keywords().get("job_titles", [])
    found = _find_matches(text or "", titles)
    if not found:
        header = detect_name_header_job(text, exclude_name=exclude_name)
        return (header, "") if header else ("", "")
    seen: list[str] = []
    for _, title in sorted(found, key=lambda t: t[0]):
        key = title.lower()
        if key not in [s.lower() for s in seen]:
            seen.append(title)
        if len(seen) >= 2:
            break
    main = seen[0] if seen else ""
    secondary = seen[1] if len(seen) > 1 else ""
    return main, secondary


def detect_name_header_job(text: str, exclude_name: str = "") -> str:
    """Fallback: treat a short capitalized header line as the requested role."""
    skip = _tokenize_norm(exclude_name or "")
    for s in (text or "").splitlines()[:10]:
        s = s.strip()
        if not s or len(s) > 45 or any(ch.isdigit() for ch in s):
            continue
        if s.lower() in _SECTION_HEADERS:
            continue
        if skip and _tokenize_norm(s) == skip:
            continue
        if _LANG.match(s) and 1 <= len(s.split()) <= 5:
            return s.title()
    return ""
def detect_skills(text: str) -> list[tuple[str, str, str]]:
    """Return (lower_token, display, category) for any profession's lexicon.

    Matching is case-insensitive. ``lower_token`` keeps lowercase for
    compatibility with scoring (which normalizes to lowercase); ``display``
    holds the canonical casing from the lexicon (nicer in the matrix).
    """
    data = load_keywords()
    low = _tokenize_norm(text or "")
    raw = text or ""
    out: list[tuple[str, str, str]] = []
    seen = set()
    for category, keywords in data.get("skills", {}).items():
        for keyword in keywords:
            key = keyword.lower()
            if key in seen:
                continue
            if len(keyword.strip()) == 1:
                # Un mot-clé d'une lettre (« r » pour le langage R) matcherait
                # n'importe quel texte : on exige une occurrence majuscule isolée
                # (« R », « C ») avant de le retenir.
                if not re.search(rf"(?<![A-Za-z0-9]){keyword.upper()}(?![A-Za-z0-9])", raw):
                    continue
                seen.add(key)
                out.append((key, keyword.upper(), category))
                continue
            pattern = rf"\b{re.escape(keyword)}\b" if keyword[0].isalnum() else re.escape(keyword)
            if re.search(pattern, low, flags=re.IGNORECASE):
                seen.add(key)
                out.append((key, keyword, category))
    return out


def detect_locations(text: str) -> list[str]:
    data = load_keywords()
    low = _tokenize_norm(text or "")
    found: list[str] = []
    for loc in data.get("locations", []):
        if loc in low and loc not in [c.lower() for c in found]:
            found.append(loc.title())
    # catch patterns such as "à Nantes", "poste à Lyon", "basé à Paris"
    for m in re.finditer(r"\b(?:à|a|basé|base|poste)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' -]{2,30})", low):
        candidate = m.group(1).strip()
        if len(candidate) >= 3 and candidate not in [c.lower() for c in found]:
            found.append(candidate.title())
    return found[:8]


def detect_contracts(text: str) -> list[str]:
    data = load_keywords()
    low = _tokenize_norm(text or "")
    out = []
    for c in data.get("contracts", []):
        if re.search(rf"\b{re.escape(c)}", low) and c not in [x.lower() for x in out]:
            out.append(c.title())
    return out


def detect_remote(text: str) -> list[str]:
    low = _tokenize_norm(text or "")
    out = []
    if any(r in low for r in ("remote", "télétravail", "teletravail")):
        out.append("remote")
    if "hybride" in low:
        out.append("hybride")
    if "sur site" in low or "présentiel" in low or "presentiel" in low:
        out.append("sur site")
    return out


def detect_years(text: str) -> float | None:
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:ans|année|années|years?)\b", (text or ""), re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def detect_sectors(text: str) -> list[str]:
    """Coarse sector detection mapping domain keywords to sector tags."""
    low = _tokenize_norm(text or "")
    sectors: list[str] = []
    mapping = {
        "data": ("data", "analytique", "machine learning", "bi "),
        "développement / informatique": ("développeur", "developeur", "backend", "frontend", "logiciel", "back end"),
        "finance": ("finance", "comptabilité", "comptable", "audit", "trésorerie"),
        "marketing / commerce": ("marketing", "commercial", "vente", "publicité", "social media"),
        "design / création": ("design", "graphisme", "ux", "ui", "illustrator"),
        "ressources humaines": ("recrutement", "rh", "paie", "gestion des talents"),
        "santé": ("santé", "soins", "infirmier", "pharmacie", "médecin"),
        "juridique": ("juridique", "droit", "contentieux", "juriste", "avocat"),
    }
    for sector, needles in mapping.items():
        if any(n in low for n in needles):
            sectors.append(sector)
    return sectors


def extract_structured(text: str) -> dict:
    """Build the canonical profile-data dict from raw CV text (generalist).

    ``skills`` est la projection plate de ``skill_matrix`` : les deux listes
    portent exactement les mêmes libellés, ce qui évite le doublon
    « sql / SQL Server » constaté dans l'interface.
    """
    name = detect_name(text)
    main_job, secondary_job = detect_jobs(text, exclude_name=name)
    jobs = [j for j in (main_job, secondary_job) if j]
    detected = detect_skills(text)
    skill_matrix = [
        {"skill": display, "category": category, "level": "à confirmer",
         "evidence": "Mot-clé détecté dans le CV"}
        for _, display, category in detected
    ]
    return {
        "name": name,
        "main_job": main_job,
        "secondary_job": secondary_job,
        "jobs": jobs,
        "adjacent_jobs": [],
        "locations": detect_locations(text),
        "mobility": "",
        "remote": detect_remote(text),
        "contracts": detect_contracts(text),
        "salary_min": None,
        "salary_max": None,
        "sectors": detect_sectors(text),
        "exclusions": [],
        "skills": [display for _, display, _ in detected],
        "years_experience": detect_years(text),
        "skill_matrix": skill_matrix,
    }