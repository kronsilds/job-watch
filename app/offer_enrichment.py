"""Complétion des champs manquants d'une annonce après scraping.

Le scraping rapporte parfois des fiches incomplètes (contrat, localisation,
salaire ou compétences absents). Cette passe comble les trous en deux temps,
toujours sans inventer de fait et sans jamais écraser une donnée déjà scrapée :

1. **Heuristique déterministe** (:func:`heuristic_completion`) — détection
   lexicale dans le titre + la description (contrats, télétravail, villes du
   lexique, fourchettes de salaire). Gratuite et hors-ligne.
2. **Passe LLM light** (:func:`enrich_offer` / :func:`enrich_offers`) — si des
   champs structurants restent vides, un modèle léger (tier ``light`` du prompt
   ``enrich_offer`` dans ``app/data/prompts.json``) extrait de l'annonce
   uniquement ce qui y figure. Déclenchée seulement quand un vrai fournisseur
   LLM est configuré, et plafonnée à ``JOBWATCH_ENRICH_LLM_MAX`` appels par
   import (garde-fou coût, défaut 25). Désactivable via ``JOBWATCH_ENRICH_LLM=0``.
"""

from __future__ import annotations

import os
import re
import unicodedata

from .heuristics import detect_contracts, detect_remote
from .llm import get_provider, DeterministicProvider
from .prompts import load_keywords

# Modalités de temps de travail présentes dans le lexique « contrats » : ce ne
# sont pas des types de contrat — on les écarte de la complétion de contrat.
_TIME_MODALITIES = {"temps plein", "plein temps", "temps partiel", "part-time"}

_FILLABLE_TEXT = ("title", "company", "location", "remote", "contract")
_FILLABLE_LIST = ("skills", "sectors")
_FILLABLE_INT = ("salary_min", "salary_max")

_SALARY_EUR = re.compile(r"(\d{2,3}(?:[ \u202f.,]\d{3})+)\s*€")
_SALARY_KEUR = re.compile(r"(\d{2,3})\s*k\s*€", re.IGNORECASE)
_SALARY_K = re.compile(r"(?<![\d.])(\d{2,3})\s*k\b", re.IGNORECASE)
_SALARY_CTX = re.compile(r"salaire|rémunération|remuneration|salary|package|brut",
                         re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def llm_enabled() -> bool:
    """La passe LLM light est-elle activée ? (``JOBWATCH_ENRICH_LLM=0`` pour couper)."""
    return (os.getenv("JOBWATCH_ENRICH_LLM", "1") or "1").strip().lower() not in (
        "0", "false", "non", "no")


def offer_text(offer: dict) -> str:
    """Texte source de l'extraction : titre + description, espaces normalisés."""
    return re.sub(r"\s+", " ", " ".join(
        str(offer.get(k) or "") for k in ("title", "description"))).strip()


def _fold(text: str) -> str:
    """Minuscules + suppression des accents (matches lexicaux tolérants)."""
    return "".join(ch for ch in unicodedata.normalize("NFD", text.lower())
                   if unicodedata.category(ch) != "Mn")


def _detect_location(text: str) -> str:
    """Ville du lexique trouvée dans le texte (accent-insensible, mot entier).

    Volontairement limitée au lexique : les motifs flous (« basé à … ») risqueraient
    de promouvoir en localisation des segments qui n'en sont pas (« à temps plein »).
    """
    low = _fold(text)
    for loc in load_keywords().get("locations", []):
        if re.search(rf"(?<![a-z]){re.escape(_fold(loc))}(?![a-z])", low):
            return loc.title()
    return ""


def _detect_salary(text: str) -> tuple[int | None, int | None]:
    """(min, max) annuels bruts détectés dans le texte, s'ils y figurent.

    Conservateur : exige le signe €, ou « k » dans un contexte de rémunération,
    pour ne pas confondre un salaire avec un chiffre quelconque (« 100k vues »).
    """
    vals: list[int] = []
    for m in _SALARY_EUR.finditer(text):  # « 80 000 € », « 80,000 € »
        vals.append(int(re.sub(r"\D", "", m.group(1))))
    for m in _SALARY_KEUR.finditer(text):  # « 45k€ »
        vals.append(int(m.group(1)) * 1000)
    if _SALARY_CTX.search(text):  # « 45k » sans € : uniquement si contexte salarial
        vals += [int(m.group(1)) * 1000 for m in _SALARY_K.finditer(text)]
    vals = [v for v in vals if 15000 <= v <= 400000]
    if not vals:
        return None, None
    return min(vals), max(vals)


def missing_fields(offer: dict) -> list[str]:
    """Champs utiles au scoring encore absents de l'offre."""
    missing = [k for k in _FILLABLE_TEXT if not str(offer.get(k) or "").strip()]
    missing += [k for k in _FILLABLE_LIST if not offer.get(k)]
    missing += [k for k in _FILLABLE_INT if offer.get(k) in (None, "")]
    return missing


def needs_completion(offer: dict) -> bool:
    """Vrai si des champs structurants manquent, justifiant une passe LLM.

    Cible les informations structurantes pour le scoring (compétences, contrat,
    localisation) ; l'absence de salaire seul ne déclenche pas d'appel (le
    salaire est légitimement absent de nombreuses annonces).
    """
    has_remote = bool(str(offer.get("remote") or "").strip())
    return (not offer.get("skills")
            or not str(offer.get("contract") or "").strip()
            or (not str(offer.get("location") or "").strip() and not has_remote))


def fill_gaps(offer: dict, extra: dict) -> dict:
    """Remplit uniquement les champs vides de ``offer`` depuis ``extra``."""
    for key in _FILLABLE_TEXT:
        if not str(offer.get(key) or "").strip():
            val = str(extra.get(key) or "").strip()
            if val:
                offer[key] = val
    for key in _FILLABLE_LIST:
        if not offer.get(key):
            vals = [str(x).strip() for x in (extra.get(key) or []) if str(x).strip()]
            if vals:
                offer[key] = vals
    for key in _FILLABLE_INT:
        if offer.get(key) in (None, ""):
            val = extra.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                offer[key] = int(val)
    return offer


def heuristic_completion(offer: dict) -> dict:
    """Complétion déterministe (gratuite, hors-ligne) des champs vides depuis le texte."""
    offer = dict(offer)
    text = offer_text(offer)
    if not text:
        return offer
    extra: dict = {}
    if not str(offer.get("contract") or "").strip():
        contracts = [c for c in detect_contracts(text)
                     if c.lower() not in _TIME_MODALITIES]
        if contracts:
            extra["contract"] = contracts[0].lower()
    if not str(offer.get("remote") or "").strip():
        remote = detect_remote(text)
        if remote:
            extra["remote"] = ", ".join(remote)
    if not str(offer.get("location") or "").strip():
        loc = _detect_location(text)
        if loc:
            extra["location"] = loc
    if offer.get("salary_min") in (None, "") or offer.get("salary_max") in (None, ""):
        smin, smax = _detect_salary(text)
        if offer.get("salary_min") in (None, "") and smin is not None:
            extra["salary_min"] = smin
        if offer.get("salary_max") in (None, "") and smax is not None:
            extra["salary_max"] = smax
    return fill_gaps(offer, extra)


def _sanitize(extra) -> dict:
    """Ne garde de la sortie LLM que des valeurs sûres et correctement typées."""
    if not isinstance(extra, dict):
        return {}
    out: dict = {}
    for key in _FILLABLE_TEXT:
        val = extra.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    for key in _FILLABLE_LIST:
        val = extra.get(key)
        if isinstance(val, list):
            vals = [str(x).strip() for x in val if str(x).strip()]
            if vals:
                out[key] = vals
    for key in _FILLABLE_INT:
        val = extra.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) \
                and 10000 <= val <= 1000000:
            out[key] = int(val)
    return out


def _real_llm(provider) -> bool:
    """Vrai si le fournisseur est un vrai LLM distant (pas le repli déterministe)."""
    return provider is not None and not isinstance(provider, DeterministicProvider)


def _llm_completion(offer: dict, provider) -> dict:
    """Passe LLM light ; n'échoue jamais (repli silencieux sur l'offre courante)."""
    try:
        return fill_gaps(offer, _sanitize(provider.enrich_offer(offer)))
    except Exception:
        return offer


def enrich_offer(offer: dict, provider=None, allow_llm: bool | None = None) -> dict:
    """Complète les champs vides d'une offre : heuristique, puis LLM light si besoin."""
    offer = heuristic_completion(offer)
    if allow_llm is None:
        allow_llm = llm_enabled()
    if not allow_llm or not needs_completion(offer):
        return offer
    provider = provider or get_provider()
    if not _real_llm(provider):
        return offer
    return _llm_completion(offer, provider)


def enrich_offers(records, provider=None, llm_budget: int | None = None):
    """Variante : même logique qu':func:`enrich_offer`, avec quota d'appels LLM par import."""
    if llm_budget is None:
        llm_budget = _env_int("JOBWATCH_ENRICH_LLM_MAX", 25)
    if provider is None and llm_enabled():
        provider = get_provider()
    remote = _real_llm(provider)
    out = []
    for rec in records:
        rec = heuristic_completion(rec)
        if remote and llm_budget > 0 and needs_completion(rec):
            llm_budget -= 1
            rec = _llm_completion(rec, provider)
        out.append(rec)
    return out
