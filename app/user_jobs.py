"""Vue personnelle des annonces : overlay JobRating sur le catalogue Job partagé."""
from __future__ import annotations

def ratings_map(db, user_id):
    from .models import JobRating
    if user_id is None:
        return {}
    return {r.job_id: r for r in db.query(JobRating).filter_by(user_id=user_id).all()}

def overlay_job(job, rating=None):
    """Copie présentation d'une annonce : note/décision personnelles si présentes."""
    score = rating.score if rating is not None and rating.score is not None else job.score
    confidence = rating.confidence if rating is not None and rating.confidence is not None else job.confidence
    decision = rating.decision if rating is not None and rating.decision else job.decision
    explanation = dict(job.explanation or {})
    if rating is not None and rating.explanation:
        base = dict(rating.explanation)
        for k in ("source", "prescore"):
            if k in explanation and k not in base:
                base[k] = explanation[k]
        explanation = base
    return score, confidence, decision, explanation

def get_or_create_rating(db, user_id, job_id, default_decision="new"):
    from .models import Job, JobRating
    job = db.get(Job, job_id)
    if job is None:
        return None, None
    if user_id is None:
        return job, None
    r = db.query(JobRating).filter_by(user_id=user_id, job_id=job_id).first()
    if r is None:
        r = JobRating(user_id=user_id, job_id=job_id, score=job.score,
                      confidence=job.confidence, decision=default_decision,
                      explanation=dict(job.explanation or {}))
        db.add(r); db.flush()
    return job, r
