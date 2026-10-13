"""Appariement normalisé des compétences entre le profil et l'offre.

Les deux côtés ne parlent pas la même langue :

* la **matrice de compétences du profil** porte des libellés riches produits par
  le LLM (« .NET Core (8-10) », « Agents IA (Claude) », « Anglais (C1) »,
  « API REST », « Git / Git Flow ») ;
* l'index d'une **offre** est bâti sur le lexique généraliste
  (``app/data/keywords.json``), donc sur des mots-clés nus (« dotnet »,
  « rest », « angular », « react », « nodejs »).

Une égalité de chaîne stricte (l'ancien comportement) rate donc presque
systématiquement, d'où des scores de compétences à 0 alors que la compétence est
bien présente des deux côtés. On compare ici des **tokens** normalisés :

* minuscules et accents retirés ;
* suffixes de niveau supprimés (« (8-10) », « (C1) », « (Claude) ») ;
* séparateurs (``/ & . , -``) éclatés en tokens (``Git / Git Flow`` → git, flow ;
  ``.NET Core`` → net, core ; ``Node.js`` → node js) ;
* les mots vides français/anglais (« de », « et », « of »…) écartés.

Deux libellés correspondent si l'un des cas suivants est vrai :

1. égalité exacte après normalisation (``SQL`` ↔ ``sql``) ;
2. égalité de la forme compactée (``Node.js`` ↔ ``nodejs``, ``CI/CD`` ↔ ``cicd``) ;
3. inclusion des tokens de l'un dans ceux de l'autre (``API REST`` ⊃ ``rest``,
   ``SQL Server`` ⊃ ``sql``, ``Anglais (C1)`` → ``anglais``) ;
4. partage d'au moins un token **significatif** (4 caractères ou plus), ce qui
   couvre « Architecture logicielle » face à « architecture » sans confondre
   ``Java`` et ``JavaScript``.
"""

from __future__ import annotations

import re
import unicodedata

_PAREN = re.compile(r"\([^)]*\)")
_TOKENS_SEP = re.compile(r"[^0-9a-z+#]+")

# Mots de liaison inutiles pour l'appariement (fr + en).
_STOPWORDS = {
    "de", "des", "du", "la", "le", "les", "un", "une", "et", "ou", "en", "au",
    "aux", "pour", "avec", "sur", "par", "the", "of", "and", "or", "to", "in",
    "for", "with", "on", "a",
}

# Tokens courts qui restent des compétences réelles (langages, domaines).
_SHORT_KEEP = {"r", "c", "go", "ai", "bi", "qa", "ui", "ux", "seo", "sem", "crm"}

# Synonymes token à token : le lexique de l'offre écrit « dotnet » là où le
# profil écrit « .NET Core », « nodejs » vs « Node.js », etc. La table est
# volontairement courte et explicite (pas de stemming automatique, qui
# confondrait « java » et « javascript »).
_ALIASES = {
    "dotnet": "net", "aspnet": "net", "netcore": "net",
    "nodejs": "node",
    "golang": "go",
    "csharp": "c#", "c-sharp": "c#",
    "k8s": "kubernetes",
    "postgres": "postgresql",
    "html5": "html", "css3": "css",
    "reactjs": "react", "angularjs": "angular", "vuejs": "vue",
    "restful": "rest", "restapi": "rest",
    # Profil francophone / annonces anglophones : mêmes concepts, deux langues.
    "ai": "ia", "agents": "agent", "agentic": "agent",
    "project": "projet", "projects": "projets",
    "team": "equipe", "teams": "equipes",
}

# Tokens trop génériques pour suffire, à eux seuls, à un appariement : ils ne
# comptent que via l'inclusion (exact/subset) ou associés à un token porteur.
_WEAK_TOKENS = {
    "core", "api", "web", "app", "data", "technique", "techniques",
    "gestion", "management", "direction",
    "metier", "metiers", "service", "services", "outil", "outils",
}


def _fold(value: str) -> str:
    """Minuscules + suppression des accents."""
    text = unicodedata.normalize("NFD", str(value or "").lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def normalize_skill(value: str) -> str:
    """Forme canonique : minuscules, sans accents/parenthèses, espaces normalisés."""
    text = _PAREN.sub(" ", _fold(value))
    return re.sub(r"\s+", " ", _TOKENS_SEP.sub(" ", text)).strip()


def _canon_token(token: str) -> str:
    """Alias puis repli du pluriel (« équipes » → ``equipe``, « teams » → ``equipe``).

    Le repli est symétrique des deux côtés de la comparaison (donc sans effet de
    bord), et ignore les mots courts ou déjà terminés par « ss » / « us ».
    """
    token = _ALIASES.get(token, token)
    if len(token) >= 5 and token.endswith("s") and not token.endswith(("ss", "us")):
        token = token[:-1]
    return token


def skill_tokens(value: str) -> set[str]:
    """Tokens significatifs d'un libellé (« Git / Git Flow » → {git, flow}).

    Les alias et le repli du pluriel sont appliqués token à token, ce qui fait
    converger « .NET Core » ({net, core}), « dotnet » ({net}) et
    « Direction d'équipes » ({direction, equipe}) / « team management ».
    """
    tokens = {
        token for token in normalize_skill(value).split()
        if token and token not in _STOPWORDS
        and (len(token) >= 2 or token in _SHORT_KEEP)
    }
    return {_canon_token(token) for token in tokens}


def _compacted(value: str) -> str:
    """Forme sans espaces ni séparateurs : « node.js » → ``nodejs``, « ci/cd » → ``cicd``."""
    return "".join(
        _canon_token(token) for token in normalize_skill(value).split()
        if token not in _STOPWORDS)


def skill_matches(left: str, right: str) -> bool:
    """Deux libellés désignent-ils la même compétence ?"""
    na, nb = normalize_skill(left), normalize_skill(right)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # « node.js » vs « nodejs », « ci/cd » vs « cicd ».
    if _compacted(left) == _compacted(right):
        return True
    ta, tb = skill_tokens(left), skill_tokens(right)
    if not ta or not tb:
        return False
    # Inclusion : « API REST » ⊇ {rest}, « SQL Server » ⊇ {sql}, « .NET Core » ⊇ {net}.
    if ta <= tb or tb <= ta:
        return True
    # Un token porteur commun suffit (« Architecture logicielle » ↔ « architecture »),
    # mais pas un token générique seul (« Direction technique » ≠ « Direction d'équipes »).
    shared = (ta & tb) - _WEAK_TOKENS
    return any(len(token) >= 4 for token in shared)


def match_skills(profile_skills, job_skills):
    """Apparie deux listes de compétences.

    Renvoie ``(matched_profile, matched_job, unmatched_profile, unmatched_job)``
    en conservant les libellés d'origine (casse lisible) et sans doublon.
    ``matched_profile`` suit l'ordre du profil (projection du scoring), ce qui
    rend le résultat stable et testable.
    """
    profiles = [str(s).strip() for s in (profile_skills or []) if str(s).strip()]
    offers = [str(s).strip() for s in (job_skills or []) if str(s).strip()]
    matched_job: set[str] = set()
    matched_profile: list[str] = []
    seen_profile: set[str] = set()
    for skill in profiles:
        key = normalize_skill(skill)
        if key in seen_profile:
            continue
        hits = [j for j in offers if j not in matched_job and skill_matches(skill, j)]
        if hits:
            seen_profile.add(key)
            matched_profile.append(skill)
            matched_job.update(hits)
    matched_offer = [j for j in offers if j in matched_job]
    unmatched_profile = [s for s in profiles if normalize_skill(s) not in seen_profile]
    unmatched_offer = [j for j in offers if j not in matched_job]
    return matched_profile, matched_offer, unmatched_profile, unmatched_offer
