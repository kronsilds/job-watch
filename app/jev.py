"""Client **Jev** (TypeSafe) via la *Decisions API* d'**OpenRouter** pour la
notation finale des offres.

Chaque dimension du scoring devient une question ``type: "score"`` notée sur
une grille de 4 niveaux décrits concrètement (0 = incompatible → 3 = adéquation
directe). Jev renvoie pour chaque question un score probabiliste (position
pondérée sur la grille) et une confiance ; la combinaison pondérée reste faite
en code (:mod:`app.scoring`), avec les poids du profil.

Une question n'est posée que si l'offre **et** le profil portent l'information
utile : une dimension non évaluable est omise (et son poids redistribué) au lieu
d'être notée au hasard.

Configuration (``.env``) :

- ``OPENROUTER_API_KEY`` — clé OpenRouter (``sk-or-...``, créée sur
  https://openrouter.ai/settings/keys) ; sans clé, le scoring reste heuristique.
  L'ancien nom ``TYPESAFE_API_KEY`` reste accepté ;
- ``JEV_BASE_URL`` — défaut ``https://openrouter.ai/api`` ; le client appelle
  ``POST {base}/alpha/decisions`` (Decisions API) ;
  l'ancien nom ``TYPESAFE_BASE_URL`` reste accepté ;
- ``JEV_MODEL`` — défaut ``typesafe/jev-1.14`` (alias ``~typesafe/jev-latest``) ;
- ``JOBWATCH_SCORER`` — ``auto`` (défaut : Jev si clé), ``jev`` ou ``heuristic`` ;
- ``JOBWATCH_JEV_WORKERS`` — requêtes parallèles (défaut 4) ;
- ``JOBWATCH_JEV_TIMEOUT`` — délai par requête en secondes (défaut 30).

Doc : https://openrouter.ai/docs/guides/community/jev-tutorial
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api"
DECISIONS_PATH = "/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.14"
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}

# Grilles de niveaux, du moins adapté (0) au plus adapté (3).
CRITERIA = {
    "intitule": [
        "Le métier de l'offre est sans lien utile avec les métiers recherchés.",
        "Une proximité générale existe, mais les missions principales relèvent d'un autre métier.",
        "Le métier visé correspond ; certaines missions restent à confirmer.",
        "Le métier et les missions correspondent directement au projet professionnel.",
    ],
    "secteur": [
        "Le secteur de l'offre est clairement différent des secteurs ciblés.",
        "Le secteur est incertain ou seulement lié de façon indirecte aux secteurs ciblés.",
        "Le secteur paraît proche d'un secteur ciblé, avec un point à confirmer.",
        "Le secteur de l'offre correspond explicitement à un secteur ciblé.",
    ],
    "competences": [
        "Les compétences explicitement demandées ne correspondent pas aux compétences documentées du profil.",
        "Seules quelques compétences générales sont transférables.",
        "Plusieurs compétences importantes correspondent, avec des écarts identifiables.",
        "Les compétences principales correspondent clairement aux preuves présentes dans le profil.",
    ],
    "experience": [
        "Le niveau ou l'expérience explicitement requis est incompatible avec le profil.",
        "Le niveau demandé semble supérieur ou différent de l'expérience documentée.",
        "L'expérience est vraisemblablement suffisante, avec des points à vérifier.",
        "Le niveau et l'expérience explicitement demandés correspondent clairement au profil.",
    ],
    "localisation": [
        "La localisation et le mode de travail sont incompatibles avec la zone et la mobilité indiquées.",
        "Un déplacement important ou une condition de mobilité incertaine est nécessaire.",
        "La localisation semble compatible, sous réserve de détails de mobilité.",
        "La localisation et le mode de travail correspondent directement aux préférences.",
    ],
    "contrat": [
        "Le type de contrat indiqué contredit les contrats recherchés.",
        "Le type de contrat est ambigu ou peu adapté aux contrats recherchés.",
        "Le type de contrat paraît compatible, avec un point à vérifier.",
        "Le type de contrat correspond explicitement à un contrat recherché.",
    ],
    "salaire": [
        "La rémunération indiquée est nettement inférieure au salaire minimum attendu.",
        "La rémunération indiquée est légèrement inférieure au salaire attendu.",
        "La rémunération indiquée est proche ou dans la fourchette attendue.",
        "La rémunération indiquée atteint ou dépasse le salaire attendu.",
    ],
}

INSTRUCTIONS = {
    "intitule": "À quel point le métier et les missions de cette offre correspondent-ils au projet professionnel du profil ?",
    "secteur": "À quel point le secteur d'activité de l'offre correspond-il aux secteurs ciblés ? "
               "Si le secteur est impossible à identifier, donne un score faible avec une confiance faible.",
    "competences": "À quel point les compétences explicitement demandées par l'offre correspondent-elles "
                   "aux compétences prouvées par le profil ?",
    "experience": "Le niveau ou l'expérience explicitement demandé par l'offre correspond-il à l'expérience "
                  "documentée dans le profil ?",
    "localisation": "La localisation et le mode de travail de l'offre correspondent-ils à la zone, au "
                    "télétravail et à la mobilité recherchés ?",
    "contrat": "Le type de contrat de l'offre correspond-il aux contrats recherchés ?",
    "salaire": "La rémunération indiquée dans l'offre correspond-elle au salaire attendu ?",
}

_SENIORITY = re.compile(
    r"\b(senior|junior|d[ée]butant|confirm[ée]|exp[ée]rience|ann[ée]es|ans|stage|alternance|"
    r"stagiaire|expert|lead|principal)\b", re.I)


def _get(job, key, default=""):
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x).strip() for x in value if str(x).strip())
    return str(value or "").strip()


def api_key() -> str:
    """Clé OpenRouter (``sk-or-...``) ; ``TYPESAFE_API_KEY`` reste accepté."""
    return (os.getenv("OPENROUTER_API_KEY") or os.getenv("TYPESAFE_API_KEY")
            or "").strip()


def base_url() -> str:
    """Racine de l'API ; le client appelle ``{base}/alpha/decisions``.

    ``TYPESAFE_BASE_URL`` est accepté comme ancien nom de ``JEV_BASE_URL``.
    """
    return (os.getenv("JEV_BASE_URL") or os.getenv("TYPESAFE_BASE_URL")
            or DEFAULT_BASE_URL).rstrip("/")


def scorer_mode() -> str:
    """``jev`` si Jev doit être utilisé, sinon ``heuristic``."""
    mode = (os.getenv("JOBWATCH_SCORER") or "auto").strip().lower()
    if mode == "heuristic":
        return "heuristic"
    if not api_key():
        return "heuristic"
    return "jev"


def model_name() -> str:
    return os.getenv("JEV_MODEL") or DEFAULT_MODEL


def workers() -> int:
    try:
        return max(1, min(16, int(os.getenv("JOBWATCH_JEV_WORKERS") or 4)))
    except ValueError:
        return 4


def build_state(job, p: dict) -> dict:
    """État structuré envoyé à Jev : le profil et l'offre, champs nommés."""
    matrix = [f"{r.get('skill')} ({r.get('level')})" for r in (p.get("skill_matrix") or [])
              if isinstance(r, dict) and r.get("skill")]
    return {
        "profil": {
            "metier_principal": _text(p.get("main_job")),
            "metiers_cibles": _text(p.get("jobs")),
            "metiers_adjacents": _text(p.get("adjacent_jobs")),
            "secteurs_cibles": _text(p.get("sectors")),
            "localisations": _text(p.get("locations")),
            "teletravail": _text(p.get("remote")),
            "mobilite": _text(p.get("mobility")),
            "contrats_cibles": _text(p.get("contracts")),
            "salaire_min": p.get("salary_min"),
            "annees_experience": p.get("years_experience"),
            "seniorite": _text(p.get("seniorite")),
            "competences": ", ".join(matrix) if matrix else _text(p.get("skills")),
            "synthese": _text(p.get("profile_summary")),
        },
        "offre": {
            "titre": _text(_get(job, "title")),
            "entreprise": _text(_get(job, "company")),
            "localisation": _text(_get(job, "location")),
            "teletravail": _text(_get(job, "remote")),
            "contrat": _text(_get(job, "contract")),
            "salaire_min": _get(job, "salary_min", None),
            "salaire_max": _get(job, "salary_max", None),
            "secteurs": _text(_get(job, "sectors", None)),
            "competences": _text(_get(job, "skills", None)),
            "description": _text(_get(job, "description"))[:6000],
        },
    }


def build_questions(state: dict) -> dict:
    """Questions posées, uniquement pour les dimensions évaluables."""
    prof, offre = state["profil"], state["offre"]
    has_offer_text = bool(offre["titre"] or offre["description"])
    has_profile_skills = bool(prof["competences"] or prof["synthese"])
    has_profile_xp = bool(prof["annees_experience"] or prof["seniorite"] or prof["synthese"])
    wanted = {
        "intitule": has_offer_text and bool(prof["metiers_cibles"] or prof["metier_principal"]),
        "secteur": has_offer_text and bool(prof["secteurs_cibles"]),
        "competences": has_profile_skills and (len(offre["description"]) >= 80 or bool(offre["competences"])),
        "experience": has_profile_xp and bool(_SENIORITY.search(offre["titre"] + " " + offre["description"])),
        "localisation": bool(offre["localisation"] or offre["teletravail"])
                        and bool(prof["localisations"] or prof["teletravail"] or prof["mobilite"]),
        "contrat": bool(offre["contrat"]) and bool(prof["contrats_cibles"]),
        "salaire": bool(offre["salaire_min"] or offre["salaire_max"]) and bool(prof["salaire_min"]),
    }
    return {k: {"type": "score", "instructions": INSTRUCTIONS[k], "criteria": CRITERIA[k]}
            for k, ok in wanted.items() if ok}


def fingerprint(payload: dict) -> str:
    """Empreinte stable d'une requête : sert de cache (pas de re-notation inutile)."""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_payload(job, p: dict) -> dict:
    state = build_state(job, p)
    return {"model": model_name(), "state": state, "questions": build_questions(state)}


def evaluate(payload: dict, client: httpx.Client | None = None, attempts: int = 3) -> dict:
    """Appelle la Decisions API OpenRouter et renvoie ``{dim: {score, max, confidence}}``.

    ``score`` est la position probabiliste sur la grille (0..max), ``max`` le
    niveau le plus haut (``len(criteria) - 1``) et ``confidence`` la confiance
    de la réponse (0 si absente). Lève une exception en cas d'échec définitif :
    l'appelant retombe alors sur l'heuristique.
    """
    if not payload.get("questions"):
        return {}
    key = api_key()
    if key and not key.startswith("sk-or-"):
        # Cause n°1 des 401 : une clé non OpenRouter (ex. clé d'un proxy LLM)
        # placée dans OPENROUTER_API_KEY/TYPESAFE_API_KEY, ou une variable
        # d'environnement obsolète qui écrase le `.env` (load_dotenv n'écrase rien).
        logger.warning(
            "La cle utilisee pour Jev ne commence pas par 'sk-or-' ; verifiez "
            "OPENROUTER_API_KEY dans .env ET l'environnement du terminal serveur "
            "(OpenRouter rejettera sinon la requete avec un 401).")
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    timeout = float(os.getenv("JOBWATCH_JEV_TIMEOUT") or 30)
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        for attempt in range(attempts):
            resp = client.post(f"{base_url()}{DECISIONS_PATH}", json=payload, headers=headers)
            if resp.status_code in RETRY_STATUSES and attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Toujours inclure la réponse du serveur : sans elle, un simple
                # « 401 Unauthorized » ne dit pas quelle clé a été envoyée ni pourquoi.
                detail = (resp.text or "").strip()[:300]
                raise httpx.HTTPStatusError(
                    f"{exc} | reponse : {detail}" if detail else str(exc),
                    request=exc.request, response=exc.response) from None
            break
        answers = resp.json().get("answers") or {}
    finally:
        if own:
            client.close()
    out = {}
    for key, q in payload["questions"].items():
        a = answers.get(key) or {}
        if not isinstance(a.get("score"), (int, float)):
            continue
        out[key] = {"score": float(a["score"]), "max": len(q["criteria"]) - 1,
                    "confidence": float(a.get("confidence") or 0.0)}
    return out
