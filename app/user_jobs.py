"""Vue personnelle des annonces : overlay JobRating sur le catalogue Job partagé.

Le catalogue ``jobs`` est commun à tous les comptes et ne porte que des
**faits** sur l'annonce (texte, dates, statut en ligne, diffusions, provenance).
Tout ce qui dépend d'un profil — note, confiance, décision, explication
(filtres, compétences communes, pré-note) — vit dans ``job_ratings`` (une ligne
par compte et par annonce). En mode multi-comptes, un compte ne voit jamais
l'évaluation calculée pour un autre : sans notation personnelle, une note
**provisoire** est calculée à la volée sur son propre profil (heuristique,
gratuite) en attendant sa prochaine notation complète.
"""
from __future__ import annotations

# Clés de ``Job.explanation`` qui sont des faits sur l'annonce (communes à tous).
SHARED_FACTS = ("source",)


def ratings_map(db, user_id):
    from .models import JobRating
    if user_id is None:
        return {}
    return {r.job_id: r for r in db.query(JobRating).filter_by(user_id=user_id).all()}


def _facts(job) -> dict:
    expl = dict(getattr(job, "explanation", None) or {})
    return {k: expl[k] for k in SHARED_FACTS if expl.get(k) not in (None, "")}


def overlay_job(job, rating=None, user_id=None, profile=None):
    """``(score, confidence, decision, explanation)`` de l'annonce pour un compte.

    * notation personnelle présente → elle seule (plus les faits partagés) ;
    * mode mono-utilisateur / historique (``user_id`` None) → l'annonce porte
      la notation, comme avant les comptes ;
    * compte sans notation personnelle → note provisoire sur ``profile``
      (profil validé du compte), ou aucune note sans profil validé.
    """
    decision = rating.decision if rating is not None and rating.decision else None
    if rating is not None and rating.score is not None:
        explanation = {**dict(rating.explanation or {}), **{
            k: v for k, v in _facts(job).items() if k not in (rating.explanation or {})}}
        return rating.score, rating.confidence, decision or "new", explanation
    if user_id is None:
        return (job.score, job.confidence, decision or job.decision,
                dict(job.explanation or {}))
    facts = _facts(job)
    if profile:
        from .scoring import score_job
        score, confidence, explanation = score_job(job, profile)
        return score, confidence, decision or "new", {**explanation, **facts,
                                                      "provisional": True}
    return None, None, decision or "new", facts


def get_or_create_rating(db, user_id, job_id, default_decision="new"):
    """Notation personnelle (créée vide au besoin : jamais copiée d'un autre compte)."""
    from .models import Job, JobRating
    job = db.get(Job, job_id)
    if job is None:
        return None, None
    if user_id is None:
        return job, None
    r = db.query(JobRating).filter_by(user_id=user_id, job_id=job_id).first()
    if r is None:
        r = JobRating(user_id=user_id, job_id=job_id, score=None, confidence=None,
                      decision=default_decision, explanation=_facts(job))
        db.add(r); db.flush()
    return job, r
