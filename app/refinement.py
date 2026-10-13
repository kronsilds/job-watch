"""Boucle itérative Rédacteur ↔ Évaluateur (LangGraph).

Génère et raffine un ``pitch`` ou un ``cv_ats`` pour une annonce donnée :
le Rédacteur produit une version, l'Évaluateur la note sur 10 ; sous le
seuil (6/10), le document repart en révision avec le feedback, jusqu'à
validation ou plafond d'itérations (5). Les commentaires de l'utilisateur
relancent une révision ciblée en conservant la version courante comme base.

Graphe : START → writer → reviewer ─┬─ score ≥ seuil ────────→ save → END
                                    ├─ plafond d'itérations ─→ save (meilleure
                                    │   version + avertissement) → END
                                    └─ sinon ─────────────────→ writer
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from .applications import audit
from .db import SessionLocal
from .llm import LLMProvider, get_provider
from .models import Application, Job, Profile

logger = logging.getLogger("jobwatch.refinement")

# Seuil de validation de la note d'adéquation (/10) et plafond anti-boucle,
# surchargeables via l'environnement.
SCORE_THRESHOLD = float(os.getenv("JOBWATCH_REFINE_THRESHOLD", "6"))
MAX_ITERATIONS = int(os.getenv("JOBWATCH_REFINE_MAX_ITER", "5"))
# Tentatives par appel LLM (le provider possède déjà son repli réseau SDK→HTTP).
LLM_ATTEMPTS = int(os.getenv("JOBWATCH_REFINE_LLM_ATTEMPTS", "2"))

DocumentType = Literal["pitch", "cv_ats"]
Status = Literal["draft", "needs_revision", "validated"]

# Tâche de prompt (app/data/prompts.json) utilisée par le Rédacteur.
WRITER_TASKS: dict[str, str] = {"pitch": "writer_pitch", "cv_ats": "writer_cv_ats"}


class RefinementState(TypedDict, total=False):
    """État partagé du graphe de raffinage Rédacteur ↔ Évaluateur."""

    application_id: int                # dossier de candidature lié à l'annonce
    job_offer: dict[str, Any]          # annonce sélectionnée (titre, skills, description…)
    candidate_profile: dict[str, Any]  # faits extraits du CV (Profile.data)
    document_type: DocumentType        # "pitch" ou "cv_ats"
    current_draft: str                 # version courante du document
    score: float                       # note d'adéquation /10 de l'évaluateur
    feedback: str                      # commentaires de l'évaluateur
    user_comments: str                 # commentaires optionnels de l'utilisateur
    iteration_count: int               # nombre de rédactions effectuées
    status: Status                     # "draft" | "needs_revision" | "validated"
    best_draft: str                    # meilleure version observée (anti-régression)
    best_score: float                  # note associée à la meilleure version
    history: list[dict[str, Any]]      # journal des itérations (debug / audit)
    warnings: list[str]                # avertissements remontés à l'utilisateur


def _clamp_score(value: Any) -> float:
    """Normalise une note LLM en ``float`` borné entre 0 et 10.

    Tolère les formats courants : ``7``, ``"7"``, ``"7/10"``, ``"7,5"``.
    """
    try:
        score = float(str(value).strip().split("/")[0].replace(",", "."))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(10.0, score))


def _call_llm(fn: Callable[[], dict], label: str) -> dict:
    """Appelle le LLM avec retry à backoff exponentiel (erreurs transitoires).

    Le provider applique déjà son propre repli SDK → HTTP ; ce retry couvre
    les erreurs réseau, timeouts et réponses JSON invalides remontées malgré
    tout. Au-delà de ``LLM_ATTEMPTS`` tentatives, l'exception est propagée.
    """
    delay = 1.0
    for attempt in range(1, LLM_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= LLM_ATTEMPTS:
                logger.error("%s : échec LLM après %d tentative(s) : %s",
                             label, attempt, exc)
                raise
            logger.warning("%s : tentative %d/%d échouée (%s) — nouvel essai "
                           "dans %.1f s", label, attempt, LLM_ATTEMPTS, exc, delay)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"{label} : chemin inatteignable")  # pragma: no cover


def route_after_review(state: RefinementState) -> str:
    """Edge conditionnel après l'Évaluateur.

    → ``save`` si la note atteint le seuil (6/10) ou si le plafond
    d'itérations (5) est atteint ; sinon retour au ``writer`` avec feedback.
    """
    if state.get("score", 0.0) >= SCORE_THRESHOLD:
        return "save"
    if state.get("iteration_count", 0) >= MAX_ITERATIONS:
        return "save"
    return "writer"


def build_graph(provider: LLMProvider | None = None,
                save_fn: Callable[[RefinementState], None] | None = None):
    """Construit et compile le graphe Rédacteur ↔ Évaluateur.

    ``provider`` (LLM) et ``save_fn`` (persistance) sont injectables pour
    les tests ; par défaut, le provider configuré par l'environnement et la
    sauvegarde en base (``save_to_application``) sont utilisés.
    """
    provider = provider or get_provider()
    persist = save_fn or _default_save

    def writer_node(state: RefinementState) -> dict[str, Any]:
        """Nœud Rédacteur : génère ou régénère le document cible.

        Reçoit l'annonce, le profil candidat, la version courante et les
        feedbacks (évaluateur + utilisateur, injectés dans le prompt), puis
        incrémente ``iteration_count``. Pour ``cv_ats``, le prompt impose
        mots-clés de l'annonce, structure ATS et format sobre.
        """
        iteration = state.get("iteration_count", 0) + 1
        document_type = state.get("document_type") or "pitch"
        task = WRITER_TASKS.get(document_type, "writer_pitch")
        logger.info("Rédaction %s — itération %d/%d (dossier #%s)",
                    document_type, iteration, MAX_ITERATIONS,
                    state.get("application_id"))
        result = _call_llm(
            lambda: provider.complete_task(
                task,
                job_offer=state.get("job_offer") or {},
                candidate_profile=state.get("candidate_profile") or {},
                current_draft=state.get("current_draft")
                              or "(aucune version — première rédaction)",
                feedback=state.get("feedback") or "(aucun)",
                user_comments=state.get("user_comments") or "(aucun)",
            ),
            label="writer_node",
        )
        document = str(result.get("document") or "").strip()
        if not document:
            raise RuntimeError("Le rédacteur a renvoyé un document vide")
        history = [*state.get("history", []), {
            "iteration": iteration, "node": "writer",
            "changes": str(result.get("changes_summary") or ""),
        }]
        return {"current_draft": document, "iteration_count": iteration,
                "status": "draft", "history": history}


    def reviewer_node(state: RefinementState) -> dict[str, Any]:
        """Nœud Évaluateur : note le document sur 10 selon des critères
        explicites (pertinence, mots-clés, clarté, personnalisation, format
        ATS) ; feedback constructif et précis si la note est sous le seuil.

        Met aussi à jour la meilleure version observée (``best_draft``).
        """
        iteration = state.get("iteration_count", 0)
        result = _call_llm(
            lambda: provider.complete_task(
                "reviewer",
                job_offer=state.get("job_offer") or {},
                candidate_profile=state.get("candidate_profile") or {},
                document_type=state.get("document_type") or "pitch",
                current_draft=state.get("current_draft") or "",
                temperature=0.1,  # évaluation stricte et stable
            ),
            label="reviewer_node",
        )
        score = _clamp_score(result.get("score"))
        feedback = str(result.get("feedback") or "").strip()
        status: Status = "validated" if score >= SCORE_THRESHOLD else "needs_revision"
        best_draft = state.get("best_draft", "")
        best_score = float(state.get("best_score", 0.0))
        if score >= best_score:
            best_draft, best_score = state.get("current_draft", ""), score
        logger.info("Évaluation — itération %d : score %.1f/10 → %s",
                    iteration, score, status)
        if feedback:
            logger.info("Feedback évaluateur (itération %d) : %s",
                        iteration, feedback)
        history = [*state.get("history", []), {
            "iteration": iteration, "node": "reviewer", "score": score,
            "feedback": feedback, "criteria": result.get("criteria") or {},
        }]
        return {"score": score, "feedback": feedback, "status": status,
                "best_draft": best_draft, "best_score": best_score,
                "history": history}


    def save_node(state: RefinementState) -> dict[str, Any]:
        """Nœud Sauvegarde : enregistre le document final, lié à l'annonce.

        Si le plafond d'itérations a été atteint sans validation, la
        **meilleure version** est restaurée et un avertissement est émis.
        """
        warnings = list(state.get("warnings", []))
        final_draft = state.get("current_draft", "")
        if state.get("score", 0.0) < SCORE_THRESHOLD:
            final_draft = state.get("best_draft") or final_draft
            warnings.append(
                f"Note {state.get('score', 0.0):.1f}/10 après "
                f"{state.get('iteration_count', 0)} itération(s) : meilleure "
                "version sauvegardée, révision manuelle conseillée."
            )
            logger.warning(warnings[-1])
        payload: RefinementState = {**state, "current_draft": final_draft,
                                    "warnings": warnings}
        persist(payload)
        logger.info("Document %s sauvegardé (dossier #%s, score %.1f/10, "
                    "%d itération(s))", state.get("document_type"),
                    state.get("application_id"), state.get("score", 0.0),
                    state.get("iteration_count", 0))
        return {"current_draft": final_draft, "warnings": warnings}

    graph = StateGraph(RefinementState)
    graph.add_node("writer", writer_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("save", save_node)
    graph.add_edge(START, "writer")
    graph.add_edge("writer", "reviewer")
    graph.add_conditional_edges("reviewer", route_after_review,
                                {"writer": "writer", "save": "save"})
    graph.add_edge("save", END)
    return graph.compile()


def save_to_application(db, state: RefinementState) -> Application:
    """Persiste le document final dans le dossier de candidature existant.

    Le document est lié à l'annonce via ``Application.job_id`` : ``pitch``
    ou ``cv_after`` selon ``document_type``. Le journal des itérations
    (scores, feedbacks) est conservé dans ``Application.quality["refinement"]``
    (aucune migration de schéma) et tracé dans la table ``Audit``.
    """
    app = db.get(Application, state.get("application_id") or 0)
    if not app:
        raise ValueError("Dossier de candidature introuvable")
    document = state.get("current_draft", "")
    if state.get("document_type") == "cv_ats":
        app.cv_after = document
    else:
        app.pitch = document
    quality = dict(app.quality or {})
    quality["refinement"] = {
        "document_type": state.get("document_type"),
        "score": state.get("score", 0.0),
        "best_score": state.get("best_score", 0.0),
        "iterations": state.get("iteration_count", 0),
        "status": state.get("status", "draft"),
        "warnings": state.get("warnings", []),
        "history": state.get("history", []),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    app.quality = quality
    try:owner=getattr(app,'user_id',None)
    except Exception:owner=None
    audit(db, "refinement",
          f"Document {state.get('document_type')} raffiné pour application {app.id}",
          {"application_id": app.id, "job_id": app.job_id,
           "score": state.get("score", 0.0),
           "iterations": state.get("iteration_count", 0),
           "status": state.get("status", "draft"),
           "warnings": state.get("warnings", [])},owner)
    db.commit()
    return app


def _default_save(state: RefinementState) -> None:
    """Persistance par défaut du nœud Sauvegarde (session dédiée)."""
    db = SessionLocal()
    try:
        save_to_application(db, state)
    finally:
        db.close()


def refine_payload(state: RefinementState) -> dict[str, Any]:
    """Charge utile API/CLI résumant le résultat de la boucle."""
    return {
        "application_id": state.get("application_id"),
        "document_type": state.get("document_type"),
        "document": state.get("current_draft", ""),
        "score": state.get("score", 0.0),
        "iterations": state.get("iteration_count", 0),
        "status": state.get("status", "draft"),
        "warnings": state.get("warnings", []),
        "history": state.get("history", []),
    }


def _job_offer_dict(job: Job) -> dict[str, Any]:
    """Vue sérialisable de l'annonce pour les prompts."""
    return {
        "id": job.id, "title": job.title or "", "company": job.company or "",
        "location": job.location or "", "remote": job.remote or "",
        "contract": job.contract or "", "skills": list(job.skills or []),
        "description": job.description or "", "url": job.url or "",
    }


def _load_context(db, application_id: int):
    """Charge le dossier, l'annonce et le profil, avec vérifs métier."""
    app = db.get(Application, application_id)
    if not app:
        raise ValueError("Dossier introuvable")
    job = db.get(Job, app.job_id)
    profile = db.get(Profile, app.profile_id)
    if not job or not profile:
        raise ValueError("Offre ou profil introuvable")
    return app, job, profile


def _initial_state(app: Application, job: Job, profile: Profile,
                   document_type: DocumentType, user_comments: str = "",
                   current_draft: str = "") -> RefinementState:
    """État initial du graphe (nouvelle session de raffinage)."""
    if document_type not in WRITER_TASKS:
        raise ValueError("document_type doit être 'pitch' ou 'cv_ats'")
    return RefinementState(
        application_id=app.id,
        job_offer=_job_offer_dict(job),
        candidate_profile=dict(profile.data or {}),
        document_type=document_type,
        current_draft=current_draft,
        score=0.0,
        feedback="",
        user_comments=user_comments,
        iteration_count=0,
        status="draft",
        best_draft="",
        best_score=0.0,
        history=[],
        warnings=[],
    )


def run_refinement(db, application_id: int, document_type: DocumentType = "pitch",
                   user_comments: str = "",
                   provider: LLMProvider | None = None) -> RefinementState:
    """Exécute la boucle complète Rédacteur ↔ Évaluateur pour un dossier.

    Le document final (validé ou meilleure version au plafond) est persisté
    dans le dossier de candidature, lié à l'annonce.
    """
    app, job, profile = _load_context(db, application_id)
    state = _initial_state(app, job, profile, document_type,
                           user_comments=user_comments.strip())
    graph = build_graph(provider=provider,
                        save_fn=lambda s: save_to_application(db, s))
    return graph.invoke(state)


def revise_with_user_comments(db, application_id: int, comments: str,
                              document_type: DocumentType | None = None,
                              provider: LLMProvider | None = None) -> RefinementState:
    """Relance le Rédacteur avec des commentaires utilisateur.

    La version courante (``pitch`` ou ``cv_after`` du dossier, selon le type
    du dernier raffinage connu) est conservée comme base de révision et les
    commentaires sont injectés dans le prompt du Rédacteur.
    """
    if not comments or not comments.strip():
        raise ValueError("Commentaires utilisateur vides")
    app, job, profile = _load_context(db, application_id)
    previous = (app.quality or {}).get("refinement") or {}
    doc_type = document_type or previous.get("document_type") or "pitch"
    if doc_type not in WRITER_TASKS:
        raise ValueError("document_type doit être 'pitch' ou 'cv_ats'")
    current = (app.cv_after if doc_type == "cv_ats" else app.pitch) or ""
    state = _initial_state(app, job, profile, doc_type,
                           user_comments=comments.strip(),
                           current_draft=current)
    graph = build_graph(provider=provider,
                        save_fn=lambda s: save_to_application(db, s))
    return graph.invoke(state)





