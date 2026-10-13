"""Tests de la boucle Rédacteur ↔ Évaluateur (LangGraph), LLM mocké.

Couvre : le routage conditionnel (score < 6 / ≥ 6 / plafond), la limite de
5 itérations avec sauvegarde de la meilleure version, l'injection des
commentaires utilisateur, le retry LLM et la persistance en base.
"""
import pytest

from app import refinement
from app.db import Base
from app.llm import DeterministicProvider
from app.models import Application, Audit, Job, Profile
from app.refinement import (build_graph, revise_with_user_comments,
                            route_after_review, run_refinement)


class FakeProvider:
    """LLM mocké : documents et notes scriptés, appels enregistrés."""

    def __init__(self, writer_docs=None, review_scores=None, failures=0):
        self.writer_docs = list(writer_docs or ["Document v1"])
        self.review_scores = list(review_scores or [8])
        self.failures = failures  # erreurs transitoires avant de réussir
        self.calls = []           # (task, kwargs) pour vérifier l'injection

    def complete_task(self, task, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("réseau simulé")
        self.calls.append((task, kwargs))
        if task.startswith("writer"):
            doc = self.writer_docs.pop(0) if self.writer_docs else "Document final"
            return {"document": doc, "changes_summary": "mock"}
        score = self.review_scores.pop(0) if self.review_scores else 8
        return {"score": score, "feedback": f"feedback {score}", "criteria": {}}


def base_state(**over):
    """État initial minimal valide, surchargeable."""
    state = {
        "application_id": 0,
        "job_offer": {"title": "Dev Python", "company": "Acme",
                      "skills": ["python"]},
        "candidate_profile": {"skills": ["python", "sql"]},
        "document_type": "pitch", "current_draft": "", "score": 0.0,
        "feedback": "", "user_comments": "", "iteration_count": 0,
        "status": "draft", "best_draft": "", "best_score": 0.0,
        "history": [], "warnings": [],
    }
    state.update(over)
    return state


def setup_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    e = create_engine("sqlite:///" + str(tmp_path / "t.db"))
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def seed_application(d):
    p = Profile(status="validated", data={"skills": ["python", "sql"],
                                          "main_job": "Dev Python"})
    j = Job(external_id="x1", title="Dev Python", company="Acme",
            skills=["python"], description="Poste Python.")
    d.add_all([p, j]); d.commit()
    a = Application(job_id=j.id, profile_id=p.id, state="draft")
    d.add(a); d.commit()
    return a


# --- Routage conditionnel -------------------------------------------------

def test_route_score_below_threshold_returns_to_writer():
    assert route_after_review(base_state(score=5.9, iteration_count=1)) == "writer"

def test_route_score_at_threshold_goes_to_save():
    assert route_after_review(base_state(score=6, iteration_count=1)) == "save"
    assert route_after_review(base_state(score=10, iteration_count=3)) == "save"

def test_route_max_iterations_goes_to_save():
    assert route_after_review(
        base_state(score=2, iteration_count=refinement.MAX_ITERATIONS)) == "save"


# --- Boucle complète (graphe + LLM mocké) ---------------------------------

def test_loop_converges_and_saves_once():
    """Score 4 (< 6) → retour Rédacteur, puis 8 (≥ 6) → sauvegarde unique."""
    provider = FakeProvider(writer_docs=["Brouillon 1", "Brouillon 2"],
                            review_scores=[4, 8])
    saves = []
    graph = build_graph(provider=provider, save_fn=lambda s: saves.append(dict(s)))
    final = graph.invoke(base_state())
    assert final["iteration_count"] == 2
    assert final["score"] == 8
    assert final["status"] == "validated"
    assert final["current_draft"] == "Brouillon 2"
    assert len(saves) == 1 and saves[0]["current_draft"] == "Brouillon 2"
    # Le feedback de l'évaluateur a bien été injecté dans la 2e rédaction.
    writer_calls = [kw for task, kw in provider.calls if task == "writer_pitch"]
    assert len(writer_calls) == 2
    assert writer_calls[1]["feedback"] == "feedback 4"
    assert writer_calls[1]["current_draft"] == "Brouillon 1"


def test_max_iterations_keeps_best_version_and_warns():
    """Plafond de 5 itérations : meilleure version sauvegardée + avertissement."""
    provider = FakeProvider(
        writer_docs=[f"v{i}" for i in range(1, 7)],
        review_scores=[5, 4, 3, 4, 2])  # toujours < 6, meilleur = v1 (5/10)
    saves = []
    graph = build_graph(provider=provider, save_fn=lambda s: saves.append(dict(s)))
    final = graph.invoke(base_state())
    assert final["iteration_count"] == refinement.MAX_ITERATIONS == 5
    assert final["status"] == "needs_revision"
    # Meilleure version (v1, 5/10) restaurée malgré une dernière version à 2/10.
    assert final["current_draft"] == "v1"
    assert final["best_score"] == 5
    assert any("meilleure version" in w for w in final["warnings"])
    assert len(saves) == 1 and saves[0]["current_draft"] == "v1"
    # 5 rédactions + 5 évaluations, pas de 6e appel.
    assert len([1 for t, _ in provider.calls if t.startswith("writer")]) == 5
    assert len([1 for t, _ in provider.calls if t == "reviewer"]) == 5


def test_user_comments_injected_with_current_draft_as_base():
    """Les commentaires utilisateur et la version courante atteignent le Rédacteur."""
    provider = FakeProvider(review_scores=[9])
    graph = build_graph(provider=provider, save_fn=lambda s: None)
    graph.invoke(base_state(current_draft="Pitch actuel",
                            user_comments="Mets l'accent sur SQL"))
    task, kwargs = provider.calls[0]
    assert task == "writer_pitch"
    assert kwargs["user_comments"] == "Mets l'accent sur SQL"
    assert kwargs["current_draft"] == "Pitch actuel"


def test_writer_retries_on_transient_failure(monkeypatch):
    """Un échec réseau transitoire est retenté (backoff neutralisé)."""
    monkeypatch.setattr(refinement.time, "sleep", lambda s: None)
    provider = FakeProvider(review_scores=[8], failures=1)
    graph = build_graph(provider=provider, save_fn=lambda s: None)
    final = graph.invoke(base_state())
    assert final["status"] == "validated" and final["iteration_count"] == 1


def test_writer_task_switches_for_cv_ats():
    """document_type='cv_ats' → tâche de prompt 'writer_cv_ats'."""
    provider = FakeProvider(review_scores=[9])
    graph = build_graph(provider=provider, save_fn=lambda s: None)
    graph.invoke(base_state(document_type="cv_ats"))
    assert provider.calls[0][0] == "writer_cv_ats"


def test_writer_empty_document_raises():
    """Un document vide du LLM est une erreur explicite."""
    provider = FakeProvider(writer_docs=["  "], review_scores=[9])
    graph = build_graph(provider=provider, save_fn=lambda s: None)
    with pytest.raises(Exception):
        graph.invoke(base_state())


# --- Persistance et points d'entrée (DB isolée) ---------------------------

def test_run_refinement_persists_to_application(tmp_path):
    d = setup_db(tmp_path)
    a = seed_application(d)
    provider = FakeProvider(writer_docs=["CV ATS final"], review_scores=[9])
    final = run_refinement(d, a.id, "cv_ats", provider=provider)
    assert final["status"] == "validated"
    d.expire_all()
    app = d.get(Application, a.id)
    assert app.cv_after == "CV ATS final" and app.pitch == ""
    ref = app.quality["refinement"]
    assert ref["score"] == 9 and ref["iterations"] == 1
    assert ref["document_type"] == "cv_ats" and ref["status"] == "validated"
    assert len(ref["history"]) == 2  # writer + reviewer
    audit = d.query(Audit).filter_by(kind="refinement").one()
    assert audit.metadata_json["job_id"] == app.job_id


def test_revise_with_user_comments_uses_current_draft(tmp_path):
    d = setup_db(tmp_path)
    a = seed_application(d)
    a.pitch = "Pitch V1"
    a.quality = {"refinement": {"document_type": "pitch"}}
    d.commit()
    provider = FakeProvider(writer_docs=["Pitch V2"], review_scores=[8])
    final = revise_with_user_comments(d, a.id, "Ton plus direct", provider=provider)
    task, kwargs = provider.calls[0]
    assert kwargs["current_draft"] == "Pitch V1"
    assert kwargs["user_comments"] == "Ton plus direct"
    d.expire_all()
    assert d.get(Application, a.id).pitch == "Pitch V2"
    assert final["status"] == "validated"


def test_revise_rejects_empty_comments(tmp_path):
    d = setup_db(tmp_path)
    a = seed_application(d)
    with pytest.raises(ValueError):
        revise_with_user_comments(d, a.id, "   ", provider=FakeProvider())


def test_run_refinement_rejects_unknown_application(tmp_path):
    d = setup_db(tmp_path)
    with pytest.raises(ValueError):
        run_refinement(d, 999, "pitch", provider=FakeProvider())


def test_invalid_document_type_rejected(tmp_path):
    d = setup_db(tmp_path)
    a = seed_application(d)
    with pytest.raises(ValueError):
        run_refinement(d, a.id, "lettre", provider=FakeProvider())


# --- Repli déterministe (sans clé API) -------------------------------------

def test_deterministic_provider_runs_full_loop():
    """Sans LLM configuré, la boucle converge et couvre les mots-clés."""
    provider = DeterministicProvider()
    saves = []
    graph = build_graph(provider=provider, save_fn=lambda s: saves.append(dict(s)))
    final = graph.invoke(base_state())
    assert final["status"] == "validated"
    assert final["score"] >= 6
    assert "python" in final["current_draft"].lower()
    assert len(saves) == 1


