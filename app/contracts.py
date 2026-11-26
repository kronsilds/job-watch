"""Qualification du type de contrat et de l'expérience demandée par une offre.

Le champ « contrat » des sites est hétérogène (« CDI », « Contrat
d'apprentissage », « INTERN », « Stage - 6 mois »…) et l'expérience exigée
n'est le plus souvent écrite que dans la description. Ce module ramène ces
indices à une taxonomie fixe et confronte l'offre au profil :

* :func:`qualify_contract` → types canoniques (``cdi``, ``cdd``, ``stage``,
  ``alternance``, ``freelance``, ``interim``, ``vie``, ``saisonnier``) ;
* :func:`extract_experience` → années minimales demandées, niveau (junior /
  senior / lead) et mention « débutant accepté » ;
* :func:`assess` → filtres ``contrat`` et ``expérience`` (True / False / None)
  et adéquation 0..1 de l'expérience, utilisés par le scoring.

Tout est déterministe, gratuit et hors-ligne ; rien n'est deviné quand
l'annonce est muette (``None`` = inconnu, jamais bloquant).
"""

from __future__ import annotations

import re
import unicodedata

CONTRACT_TYPES = ("cdi", "cdd", "stage", "alternance", "freelance", "interim",
                  "vie", "saisonnier")
LABELS = {"cdi": "CDI", "cdd": "CDD", "stage": "Stage", "alternance": "Alternance",
          "freelance": "Freelance", "interim": "Intérim", "vie": "VIE",
          "saisonnier": "Saisonnier"}
# Contrats « étudiants » : un profil qui ne cherche que ceux-là n'est pas
# compatible avec une offre qui exige plusieurs années d'expérience.
STUDENT_TYPES = frozenset({"stage", "alternance"})

# Seuil d'expérience au-delà duquel une offre n'est pas un stage/une alternance.
STUDENT_MAX_YEARS = 2.0

# schema.org ``employmentType`` (temps plein/partiel ne sont pas des contrats).
_EMPLOYMENT_TYPES = {"intern": "stage", "internship": "stage",
                     "apprentice": "alternance", "apprenticeship": "alternance",
                     "contractor": "freelance", "temporary": "cdd",
                     "seasonal": "saisonnier", "permanent": "cdi"}


def fold(text) -> str:
    """Minuscules sans accents, apostrophes typographiques normalisées."""
    text = str(text or "").replace("’", "'").lower()
    return "".join(ch for ch in unicodedata.normalize("NFD", text)
                   if unicodedata.category(ch) != "Mn")


# Motifs « libéraux » : pour un champ contrat ou un intitulé, où la simple
# mention du mot suffit (« Alternance - Développeur », « Stagiaire data »).
_LIBERAL = [
    ("alternance", r"\balternan\w*|\bapprenti\w*|\bapprentissage\b|"
                   r"\bprofessionnalisation\b|\bcontrat pro\b|\bwork[- ]study\b"),
    ("stage", r"\bstages?\b|\bstagiaires?\b|\binternships?\b|\bintern\b"),
    ("vie", r"\bvolontariat international\b"),
    ("freelance", r"\bfree[- ]?lances?\b|\bindependant\w*|\bportage salarial\b|"
                  r"\bcontractor\b|\bmission freelance\b"),
    ("interim", r"\binterim\w*|\btravail temporaire\b"),
    ("saisonnier", r"\bsaisonni\w*|\bseasonal\b"),
    ("cdd", r"\bcdd\b|duree determinee|\bfixed[- ]term\b"),
    ("cdi", r"\bcdi\b|duree indeterminee|\bpermanent\b"),
]
# Motifs « stricts » pour une description : il faut une tournure de contrat,
# pas une simple occurrence (« vous encadrerez des alternants », « travail
# indépendant », « après un premier stage » ne qualifient pas l'offre).
_STRICT = [
    ("alternance", r"\ben alternance\b|\bcontrat d'apprentissage\b|"
                   r"\bcontrat de professionnalisation\b|\brythme (?:d'alternance|alterne)\b|"
                   r"\boffre d'alternance\b|\bapprentissage de \d+\s*(?:mois|ans?)\b|"
                   r"\bwork[- ]study\b|\bapprenticeship\b"),
    ("stage", r"\bstage de \d+\s*(?:a \d+\s*)?mois\b|\bstage de fin d'etudes\b|"
              r"\bconvention de stage\b|\boffre de stage\b|\bstage conventionne\b|"
              r"\b\d+[- ]month internship\b|\binternship of \d+\b"),
    ("vie", r"\bvolontariat international\b"),
    ("freelance", r"\bmission freelance\b|\ben freelance\b|\bstatut (?:freelance|independant)\b|"
                  r"\bportage salarial\b"),
    ("interim", r"\bmission d'interim\b|\ben interim\b|\bcontrat d'interim\b"),
    ("saisonnier", r"\bcontrat saisonnier\b|\bemploi saisonnier\b"),
    ("cdd", r"\bcdd\b|\bcontrat a duree determinee\b|\bfixed[- ]term contract\b"),
    ("cdi", r"\bcdi\b|\bcontrat a duree indeterminee\b|\bpermanent contract\b"),
]
_VIE_ACRONYM = re.compile(r"(?<![A-Za-z])V\.?I\.?E\.?(?![A-Za-z])")  # « VIE », pas « vie »


def _matches(text: str, patterns, raw: str = "") -> list[str]:
    low = fold(text)
    out = [kind for kind, pat in patterns if re.search(pat, low)]
    if raw and _VIE_ACRONYM.search(raw) and "vie" not in out:
        out.append("vie")
    return out


def _student_dominance(types: list[str]) -> list[str]:
    """« CDD + apprentissage » est une alternance : le contrat étudiant l'emporte."""
    if any(t in STUDENT_TYPES for t in types):
        types = [t for t in types if t not in ("cdi", "cdd")]
    seen: list[str] = []
    for t in types:
        if t not in seen:
            seen.append(t)
    return seen


def normalize_contract(value) -> list[str]:
    """Types canoniques d'un champ contrat (chaîne, liste ou ``employmentType``)."""
    values = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        key = fold(text).replace("-", "_").replace(" ", "_")
        if key in _EMPLOYMENT_TYPES:
            out.append(_EMPLOYMENT_TYPES[key])
            continue
        if key in CONTRACT_TYPES:
            out.append(key)
            continue
        out += _matches(text, _LIBERAL, raw=text)
    return _student_dominance(out)


def qualify_contract(title="", description="", contract="") -> list[str]:
    """Types de contrat d'une offre (liste ordonnée, vide si inconnu).

    Priorité au champ contrat et à l'intitulé (motifs libéraux) ; la
    description n'est lue qu'à défaut, avec des tournures strictes. Une offre
    « stage ou alternance » renvoie les deux types.
    """
    found = normalize_contract(contract) + _matches(title, _LIBERAL, raw=str(title or ""))
    if not found:
        found = _matches(description, _STRICT, raw="")
    return _student_dominance(found)


def labels(types) -> str:
    """« Stage, Alternance » pour l'affichage."""
    return ", ".join(LABELS.get(t, t) for t in (types or []))


# ---------------------------------------------------------------------------
# Expérience demandée
# ---------------------------------------------------------------------------

_YEARS = re.compile(
    r"(?<![\d.,])(\d{1,2}(?:[.,]5)?)\s*\+?\s*(?:(?:a|-|–|to|ou)\s*\d{1,2}\s*)?"
    r"(?:ans?\b|an\(s\)|annees?\b|years?\b|yrs?\b)")
_XP_WORD = re.compile(r"experi[ea]nce|\bexp\b|\bxp\b")
# Contextes où un nombre d'années n'est pas une expérience exigée.
_NOT_XP = re.compile(
    r"(?:moins de|\bagee?s?(?: de)?|\bage(?: de)?|under|older than|depuis|il y a|"
    r"creee?(?: il y a| en)?|fondee?(?: il y a| en)?)\s*$")
_BEGINNER = re.compile(
    r"debutants? (?:\(?e?s?\)?\s*)?acceptes?|debutant\(e\) accepte|jeunes? diplomes?|"
    r"premiere experience|sans experience|aucune experience (?:n'est )?(?:requise|exigee)|"
    r"\bentry[- ]level\b|\bgraduate\b|\bno experience required\b")
_LEVELS = [
    ("lead", r"\blead\b|\bprincipal\b|\bstaff\b|\bhead of\b|\bdirect(?:eur|rice|or)\b|"
             r"\bchef de (?:service|equipe)\b|\bvp\b"),  # « chef de projet » : un métier, pas un niveau
    ("senior", r"\bsenior\b|\bsr\b|\bconfirme\w*|\bexperimente\w*|\bexpert\w*"),
    ("junior", r"\bjunior\b|\bjr\b|\bdebutant\w*|\bjeune diplome\w*|\bgraduate\b|"
               r"\bentry[- ]level\b"),
]


# Niveau exigé écrit en toutes lettres dans une description (« profil senior »,
# « poste junior », « niveau : confirmé ») ; une simple occurrence (« vous
# travaillerez avec des experts », « le lead vous encadre ») ne compte pas.
_DESC_LEVEL = re.compile(
    r"\b(?:profil|poste|niveau|candidat\w*|vous etes)\s*(?:de\s*)?:?\s*"
    r"(junior|senior|confirme\w*|experimente\w*|debutant\w*)"
    r"|\b(junior|senior|debutant\w*)s? (?:accepte|exige|requis|souhaite)")
_DESC_LEVEL_MAP = {"junior": "junior", "debutant": "junior", "senior": "senior",
                   "confirme": "senior", "experimente": "senior"}


def _desc_level(text: str) -> str | None:
    m = _DESC_LEVEL.search(fold(text))
    if not m:
        return None
    word = m.group(1) or m.group(2)
    return next((lvl for stem, lvl in _DESC_LEVEL_MAP.items() if word.startswith(stem)), None)


def _level_in(text: str) -> str | None:
    low = fold(text)
    for level, pat in _LEVELS:
        if re.search(pat, low):
            return level
    return None


def extract_experience(title="", description="") -> dict:
    """Expérience exigée par l'offre : ``{min_years, level, beginner_ok}``.

    ``min_years`` ne retient qu'un nombre d'années accolé à « expérience »
    (fenêtre courte autour du nombre), jamais un âge (« moins de 30 ans ») ni
    l'ancienneté de l'entreprise. ``level`` vient de l'intitulé, sinon de la
    description ; ``beginner_ok`` signale « débutant accepté », « jeune
    diplômé », « première expérience »… Le niveau lu dans la description
    exige une tournure explicite (« profil senior »), pas un simple mot.
    """
    low = fold(f"{title or ''} \n {description or ''}")
    years: list[float] = []
    for m in _YEARS.finditer(low):
        before = low[max(0, m.start() - 45):m.start()]
        after = low[m.end():m.end() + 30]
        if not (_XP_WORD.search(before) or _XP_WORD.search(after)):
            continue
        if _NOT_XP.search(before):
            continue
        value = float(m.group(1).replace(",", "."))
        if 0 < value <= 30:
            years.append(value)
    level = _level_in(title) or _desc_level(description)
    beginner = bool(_BEGINNER.search(low))
    if beginner and level is None:
        level = "junior"
    return {"min_years": min(years) if years else None, "level": level,
            "beginner_ok": beginner}


def experience_label(xp: dict) -> str:
    """Résumé lisible : « 3 ans minimum · senior », « débutant accepté »…"""
    parts = []
    if xp.get("min_years") is not None:
        parts.append(f"{xp['min_years']:g} an(s) minimum")
    if xp.get("beginner_ok"):
        parts.append("débutant accepté")
    elif xp.get("level"):
        parts.append(xp["level"])
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Confrontation offre ↔ profil
# ---------------------------------------------------------------------------

def _get(job, key, default=""):
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def profile_targets(profile: dict) -> set[str]:
    """Types de contrat recherchés par le profil (taxonomie canonique)."""
    return set(normalize_contract(list((profile or {}).get("contracts") or [])))


def student_mode(profile: dict) -> bool:
    """Vrai si le profil ne cherche QUE des stages et/ou alternances."""
    targets = profile_targets(profile)
    return bool(targets) and targets <= STUDENT_TYPES


def _profile_years(profile: dict) -> float | None:
    years = (profile or {}).get("years_experience")
    if isinstance(years, (int, float)) and not isinstance(years, bool) and years >= 0:
        return float(years)
    return None


def _profile_levels(profile: dict) -> set[str]:
    raw = (profile or {}).get("seniorite") or []
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]+", raw)
    return {lvl for lvl in (_level_in(str(x)) for x in raw) if lvl}


def assess(job, profile: dict) -> dict:
    """Filtres contrat/expérience d'une offre pour un profil.

    Retourne ``{contract_types, contract_ok, experience, experience_ok,
    experience_fit, reasons}`` où les filtres valent True (compatible), False
    (incompatibilité explicite) ou None (donnée absente : jamais bloquant).
    """
    title = str(_get(job, "title", "") or "")
    description = str(_get(job, "description", "") or "")
    types = qualify_contract(title, description, _get(job, "contract", ""))
    xp = extract_experience(title, description)
    targets = profile_targets(profile)
    reasons: list[str] = []

    contract_ok = None
    if types:
        if not targets:
            # Aucun contrat exploitable au profil : on garde l'ancien critère
            # (sous-chaîne sur le texte libre), sinon pas de contrainte.
            free = [fold(x) for x in ((profile or {}).get("contracts") or []) if str(x).strip()]
            raw = fold(_get(job, "contract", ""))
            contract_ok = (not free or not raw or
                           any(c in raw or raw in c for c in free))
        else:
            contract_ok = bool(set(types) & targets)
            if not contract_ok:
                reasons.append(f"contrat {labels(types)} hors des contrats recherchés "
                               f"({labels(sorted(targets))})")

    experience_ok = None
    fit = None
    years = _profile_years(profile)
    min_years, level = xp["min_years"], xp["level"]
    if student_mode(profile):
        # Une offre explicitement en stage/alternance s'adresse à des étudiants :
        # seul un nombre d'années exigé la rend incompatible, pas l'intitulé
        # (« Alternance - Tech Lead », « Chef de projet digital en alternance »).
        student_offer = bool(types) and set(types) <= STUDENT_TYPES
        too_much = (min_years is not None and min_years >= STUDENT_MAX_YEARS) or \
            (level in ("senior", "lead") and not student_offer)
        if too_much:
            experience_ok, fit = False, 0.0
            reasons.append("expérience exigée ("
                           + (experience_label(xp) or level or "")
                           + ") incompatible avec un stage ou une alternance")
        elif min_years is not None or xp["beginner_ok"] or level == "junior" or \
                (types and set(types) <= STUDENT_TYPES):
            experience_ok, fit = True, 1.0
    else:
        profile_levels = _profile_levels(profile)
        if min_years is not None and years is not None:
            gap = min_years - years
            fit = 1.0 if gap <= 0 else (0.5 if gap <= 2 else 0.0)
            experience_ok = gap < 4
            if not experience_ok:
                reasons.append(f"{min_years:g} an(s) d'expérience exigés pour "
                               f"{years:g} an(s) au profil")
        if level in ("senior", "lead") and years is not None and years < 2:
            experience_ok, fit = False, 0.0
            reasons.append(f"poste {level} pour {years:g} an(s) d'expérience")
        elif level and profile_levels and level not in profile_levels \
                and experience_ok is not False:
            order = ["junior", "senior", "lead"]
            if max(order.index(p) for p in profile_levels) < order.index(level):
                fit = 0.3 if fit is None else min(fit, 0.3)
        if level == "junior" and years is not None and years >= 8:
            fit = 0.5 if fit is None else min(fit, 0.5)  # surqualification : pénalité douce
        if fit is None and (min_years is not None or level) and years is not None:
            fit = 1.0
    return {"contract_types": types, "contract_ok": contract_ok, "experience": xp,
            "experience_ok": experience_ok, "experience_fit": fit, "reasons": reasons}


def search_suffixes(profile: dict) -> list[str]:
    """Mots-clés de contrat à ajouter aux recherches (profil stage/alternance)."""
    if not student_mode(profile):
        return [""]
    return [t for t in ("alternance", "stage") if t in profile_targets(profile)]
