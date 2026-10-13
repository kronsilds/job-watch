"""Scoring pondéré 0-10 des offres, avec poids configurables depuis le profil.

Le poids par dimension (compétences, intitulé, localisation, contrat, salaire)
est lu depuis ``profile.data["ponderation"]`` (ou ``weights``) avec des valeurs
par défaut stables. Les filtres "mandatory" (exclusions, intitulé, localisation,
contrat, salaire demandé) plafonnent le score à 3.9 tant qu'ils ne sont pas
tous respectés — l'humain doit valider avant toute candidature.

La dimension « compétences » mesure la **part de la demande de l'offre couverte
par le profil**, l'appariement se faisant sur des tokens normalisés
(:mod:`app.skills`) et non sur une égalité de chaîne.

Notation finale (:func:`score_all`) : quand une clé TypeSafe est configurée,
chaque dimension est notée par **Jev** (primitive *Score*, :mod:`app.jev`) puis
combinée ici avec les mêmes poids. :func:`score_job` reste l'heuristique
gratuite (pré-note, repli sans clé ou en cas d'erreur réseau).
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import jev
from .skills import match_skills

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {
    "competences": 0.35,
    "intitule": 0.15,
    "experience": 0.10,
    "localisation": 0.15,
    "contrat": 0.10,
    "salaire": 0.10,
    "secteur": 0.05,
}
# Dimensions dont un « vrai » conflit (niveau 0 net) plafonne la note à 3.9.
GATE_DIMS = {"intitule": "intitulé", "localisation": "localisation",
             "contrat": "contrat", "salaire": "salaire"}


def _weights(p: dict, keys=None) -> dict:
    """Poids du profil, normalisés (somme == 1) sur les dimensions ``keys``
    effectivement évaluées : le poids d'une dimension absente est redistribué."""
    tots = p.get("ponderation") or p.get("weights") or {}
    keys = list(keys or DEFAULT_WEIGHTS)
    w = {k: max(0.0, float(tots.get(k, DEFAULT_WEIGHTS.get(k, 0.0)))) for k in keys}
    s = sum(w.values())
    if s <= 0:
        return {k: 1.0 / len(keys) for k in keys} if keys else {}
    return {k: v / s for k, v in w.items()}


def _get(job, key, default=""):
    """Lit un champ sur un Job ORM (attribut) ou un dict normalisé (clé)."""
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def score_job(job, p):
    n = lambda x: str(x).lower().strip()
    # Les libellés du profil (« .NET Core (8-10) », « API REST ») et ceux de
    # l'offre (lexique nu : « dotnet », « rest ») ne s'écrivent pas pareil : on
    # apparie sur des tokens normalisés, jamais sur une égalité de chaîne.
    skills = [str(x) for x in (p.get("skills") or []) if str(x).strip()]
    js = [str(x) for x in (_get(job, "skills", None) or []) if str(x).strip()]
    overlap, matched_offer, _unmatched_profile, unmatched_offer = match_skills(skills, js)
    jobs = {n(x) for x in (p.get("jobs") or []) + (p.get("adjacent_jobs") or [])}
    title = n(_get(job, "title", ""))
    # Un gate vaut True (match), False (mismatch explicite) ou None (donnée absente).
    # None n'est jamais bloquant : on ne pénalise pas une offre parce qu'une info
    # n'est pas renseignée (contrat/salaire/localisation vides), on la traite en neutre.
    title_ok = (not jobs or any(x in title for x in jobs)) if title else None

    locs = {n(x) for x in p.get("locations", [])}
    remote = n(_get(job, "remote", ""))
    loc = n(_get(job, "location", ""))
    loc_ok = None
    if loc:
        # correspondance exacte OU partielle (tokens) : "Paris" doit matcher
        # "Paris 02 - 75" ou "PARIS, ÎLE-DE-FRANCE, 75001". Un vrai conflit
        # ("Lyon" vs "Paris") reste un mismatch non bloquant à neutre.
        loc_ok = (not locs) or "remote" in remote or any(
            l in loc or loc in l for l in locs)

    contracts = {n(x) for x in p.get("contracts", [])}
    cval = n(_get(job, "contract", ""))
    contract_ok = None if not cval else (
        not contracts or any(c in cval or cval in c for c in contracts))

    sal_min = p.get("salary_min")
    smax = _get(job, "salary_max", None)
    if not sal_min:
        salary_ok = True          # aucune exigence de salaire dans le profil : satisfait
    elif smax is None:
        salary_ok = None          # salaire non renseigné dans l'offre : neutre
    else:
        salary_ok = float(smax) >= float(sal_min)

    desc = n(_get(job, "description", ""))
    excluded = any(n(x) in title or n(x) in desc for x in p.get("exclusions", []))

    gates = {"intitulé": title_ok, "localisation": loc_ok, "contrat": contract_ok,
             "salaire": salary_ok, "exclusions": not excluded}
    mandatory = all(v is not False for v in gates.values())

    # Dimension « compétences » : part de la **demande de l'offre** que le profil
    # couvre. L'ancien ratio (part des compétences du profil utilisées par
    # l'offre) s'effondrait dès que la matrice était riche : avec 50 compétences
    # au profil, une offre parfaitement couverte plafonnait à ~0,1. Sans index de
    # compétences côté offre, la donnée est *inconnue* → neutre 0.5, comme tout
    # autre filtre non renseigné (on ne pénalise pas une offre sur une donnée
    # absente de la source).
    if not js:
        ratio, competences_source = 0.5, "absente"
    elif not skills:
        ratio, competences_source = 0.0, "profil vide"
    else:
        ratio, competences_source = len(matched_offer) / len(js), "offre"

    def _dim(ok):
        # True -> crédit complet, False -> zéro, None (inconnu) -> neutre 0.5
        return 1.0 if ok is True else (0.0 if ok is False else 0.5)

    dims = {
        "competences": ratio,
        "intitule": _dim(title_ok),
        "localisation": _dim(loc_ok),
        "contrat": _dim(contract_ok),
        "salaire": _dim(salary_ok),
        "secteur": 1.0 if not excluded else 0.0,
    }
    # L'heuristique ne sait pas évaluer l'expérience : son poids est redistribué.
    w = _weights(p, dims)
    raw = sum(dims[k] * w[k] for k in dims) * 10
    # Le plafond 3.9 ne s'applique qu'en cas de *vrai* conflit sur un filtre
    # obligatoire (intitulé/localisation/contrat/salaire/exclusions), pas quand
    # l'offre a simplement une donnée manquante.
    final = round(raw if mandatory else min(raw, 3.9), 1)
    confidence = round(min(1, .45 + len(overlap) * .1 + .2 * bool(title_ok)), 2)
    explanation = {
        "scorer": "heuristic",
        "mandatory": mandatory,
        "gates": gates,
        "weights": {k: round(v, 3) for k, v in w.items()},
        "dimensions": {k: round(v * 10, 1) for k, v in dims.items()},
        "strengths": sorted(overlap),
        # `gaps` = compétences **demandées par l'offre** que le profil ne couvre
        # pas (libellé d'origine de l'offre) ; `profile_gaps` = l'inverse, pour
        # ne plus confondre les deux listes dans l'interface.
        "gaps": sorted(unmatched_offer),
        "profile_gaps": sorted(_unmatched_profile),
        "competences_source": competences_source,
        "matched_offer_skills": sorted(matched_offer),
        "evidence": [f"compétences: {_get(job, 'skills', [])}",
                     f"intitulé: {_get(job, 'title', '')}",
                     f"localisation: {_get(job, 'location', '')}"],
        "reason": "Filtre obligatoire respecté" if mandatory else "Filtre obligatoire non respecté",
    }
    return final, confidence, explanation


def score_job_jev(job, p, answers: dict, meta: dict | None = None):
    """Note finale à partir des réponses Jev (``{dim: {score, max, confidence}}``).

    Les éléments déterministes (compétences communes/manquantes, exclusions)
    viennent de l'heuristique ; les dimensions, les filtres et la confiance
    viennent de Jev. Sans réponse exploitable, renvoie la note heuristique.
    """
    h_score, h_conf, h_expl = score_job(job, p)
    dims = {k: max(0.0, min(1.0, a["score"] / a["max"]))
            for k, a in (answers or {}).items() if a.get("max")}
    if not dims:
        return h_score, h_conf, h_expl
    w = _weights(p, dims)
    raw = sum(dims[k] * w[k] for k in dims) * 10
    # Filtre bloquant : Jev place l'offre nettement au niveau 0 avec assurance.
    # Une dimension non évaluée reste neutre (None), jamais bloquante.
    gates = {label: (None if k not in dims else
                     not (dims[k] < 0.25 and answers[k]["confidence"] >= 0.5))
             for k, label in GATE_DIMS.items()}
    gates["exclusions"] = h_expl["gates"]["exclusions"]
    mandatory = all(v is not False for v in gates.values())
    final = round(raw if mandatory else min(raw, 3.9), 1)
    confidence = round(sum(answers[k]["confidence"] * w[k] for k in dims), 2)
    explanation = {
        **h_expl,
        "scorer": "jev",
        # La dimension est notée par Jev sur l'annonce entière, pas sur l'index.
        "competences_source": "jev",
        "mandatory": mandatory,
        "gates": gates,
        "weights": {k: round(v, 3) for k, v in w.items()},
        "dimensions": {k: round(v * 10, 1) for k, v in dims.items()},
        "heuristic_score": h_score,
        "jev": {**(meta or {}), "answers": answers},
        "reason": "Filtre obligatoire respecté" if mandatory else "Filtre obligatoire non respecté",
    }
    return final, confidence, explanation


def _jev_answers(jobs, p, olds):
    """Interroge Jev pour chaque offre (en parallèle), avec cache par empreinte.

    Renvoie ``{job.id: (answers | None, meta)}`` ; ``answers`` vaut None en
    cas d'erreur (l'offre est alors notée par l'heuristique).
    """
    todo, results = {}, {}
    for j in jobs:
        payload = jev.build_payload(j, p)
        fp = jev.fingerprint(payload)
        meta = {"fingerprint": fp, "model": payload["model"]}
        cached = (olds.get(j.id) or {}).get("jev") or {}
        if cached.get("fingerprint") == fp and "answers" in cached:
            results[j.id] = (cached["answers"], meta)
        else:
            todo[j.id] = (payload, meta)
    if not todo:
        return results
    timeout = float(os.getenv("JOBWATCH_JEV_TIMEOUT") or 30)
    with httpx.Client(timeout=timeout) as client, ThreadPoolExecutor(jev.workers()) as pool:
        futures = {jid: pool.submit(jev.evaluate, payload, client)
                   for jid, (payload, _meta) in todo.items()}
        for jid, fut in futures.items():
            meta = todo[jid][1]
            try:
                results[jid] = (fut.result(), meta)
            except Exception as exc:  # réseau, quota, clé invalide… → repli heuristique
                logger.warning("Jev indisponible pour l'offre %s : %s", jid, exc)
                results[jid] = (None, {**meta, "error": str(exc)[:300]})
    return results


def score_all(db, p, user_id=None):
    from .models import Job, JobRating
    jobs = list(db.query(Job))
    olds = {}
    for j in jobs:
        try:
            olds[j.id] = dict(j.explanation or {})
        except Exception:
            olds[j.id] = {}
    jev_results = _jev_answers(jobs, p, olds) if jev.scorer_mode() == "jev" else {}
    for j in jobs:
        old = olds[j.id]
        answers, meta = jev_results.get(j.id, (None, {}))
        if answers:
            j.score, j.confidence, j.explanation = score_job_jev(j, p, answers, meta)
        else:
            j.score, j.confidence, j.explanation = score_job(j, p)
            if meta.get("error"):
                j.explanation = {**j.explanation, "jev_error": meta["error"]}
        # Métadonnées portées uniquement par l'import : provenance de l'annonce
        # (`source`) et pré-note de la passe 1. Le scoring (passe 2) reconstruit
        # l'explication, donc on les reporte explicitement.
        carried = {k: old[k] for k in ("prescore", "source") if old.get(k) not in (None, "")}
        if carried:
            try:
                j.explanation = {**(j.explanation or {}), **carried}
            except Exception:
                pass
    if user_id is not None:
        for j in jobs:
            r = db.query(JobRating).filter_by(user_id=user_id, job_id=j.id).first()
            if not r:
                r = JobRating(user_id=user_id, job_id=j.id); db.add(r)
            r.score = j.score; r.confidence = j.confidence
            r.explanation = dict(j.explanation or {})
            if not r.decision or r.decision not in ("new", "retained", "ignored"):
                r.decision = "new"
    db.commit()
