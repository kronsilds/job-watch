"""Notation finale par Jev (Decisions API OpenRouter) : questions, combinaison, cache, repli."""
import httpx
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import jev
from app.db import Base
from app.models import Job
from app.scoring import score_all, score_job_jev

PROFILE = {'skills': ['SQL', 'Python'], 'jobs': ['Data Analyst'], 'locations': ['Lyon'],
           'contracts': ['CDI'], 'salary_min': 45000, 'sectors': ['Banque'],
           'profile_summary': 'Data analyst, 5 ans en banque.'}
OFFER = dict(title='Data Analyst senior', company='A', location='Lyon', contract='CDI',
             salary_max=None, skills=['SQL'], sectors=[],
             description='Analyse de données bancaires, SQL et Python, 5 ans d\'expérience. ' * 3)


def _answers(**levels):
    return {k: {'score': v, 'max': 3, 'confidence': 0.9} for k, v in levels.items()}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('JOBWATCH_SCORER', 'auto')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-key')
    e = create_engine('sqlite:///' + str(tmp_path / 'jev.db'))
    Base.metadata.create_all(e)
    d = sessionmaker(bind=e)()
    d.add(Job(external_id='a', **OFFER)); d.commit()
    return d


def test_questions_only_for_evaluable_dimensions():
    q = jev.build_payload(OFFER, PROFILE)['questions']
    assert {'intitule', 'secteur', 'competences', 'experience', 'localisation', 'contrat'} <= set(q)
    assert 'salaire' not in q  # salaire absent de l'offre : dimension omise
    assert all(v['type'] == 'score' and len(v['criteria']) == 4 for v in q.values())


def test_mode_requires_key(monkeypatch):
    monkeypatch.setenv('JOBWATCH_SCORER', 'auto')
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    assert jev.scorer_mode() == 'heuristic'
    monkeypatch.setenv('OPENROUTER_API_KEY', 'k')
    assert jev.scorer_mode() == 'jev'
    monkeypatch.delenv('OPENROUTER_API_KEY')
    monkeypatch.setenv('TYPESAFE_API_KEY', 'legacy')  # ancien nom accepté
    assert jev.scorer_mode() == 'jev'


def test_combination_redistributes_weights_and_caps_on_gate():
    s, c, e = score_job_jev(OFFER, PROFILE, _answers(intitule=3, competences=3, localisation=3))
    assert s == 10.0 and e['scorer'] == 'jev' and e['mandatory']
    assert abs(sum(e['weights'].values()) - 1) < 1e-6 and set(e['weights']) == {'intitule', 'competences', 'localisation'}
    assert c == 0.9
    s, _, e = score_job_jev(OFFER, PROFILE, _answers(intitule=3, competences=3, localisation=0.2))
    assert e['gates']['localisation'] is False and s <= 3.9


def test_score_all_uses_jev_and_caches(db, monkeypatch):
    calls = []

    def fake_eval(payload, client=None):
        calls.append(payload)
        return {k: {'score': 2.4, 'max': 3, 'confidence': 0.8} for k in payload['questions']}

    monkeypatch.setattr(jev, 'evaluate', fake_eval)
    score_all(db, PROFILE)
    j = db.query(Job).first()
    assert j.explanation['scorer'] == 'jev' and j.score == 8.0 and len(calls) == 1
    score_all(db, PROFILE)  # rien n'a changé : aucune nouvelle requête
    assert len(calls) == 1
    score_all(db, {**PROFILE, 'locations': ['Paris']})  # profil modifié : re-notation
    assert len(calls) == 2


def test_score_all_falls_back_to_heuristic_on_error(db, monkeypatch):
    def boom(payload, client=None):
        raise httpx.ConnectError('down')

    monkeypatch.setattr(jev, 'evaluate', boom)
    score_all(db, PROFILE)
    j = db.query(Job).first()
    assert j.explanation['scorer'] == 'heuristic' and 'down' in j.explanation['jev_error']
    assert j.score is not None


def test_evaluate_parses_response_and_retries(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'k')
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    monkeypatch.delenv('JEV_MODEL', raising=False)
    monkeypatch.setattr(jev.time, 'sleep', lambda s: None)
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(529)
        return httpx.Response(200, json={'answers': {'intitule': {'type': 'score', 'score': 2.5, 'confidence': 0.7}}})

    payload = jev.build_payload(OFFER, PROFILE)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        out = jev.evaluate(payload, client)
    assert len(seen) == 2 and seen[-1].url.path == '/api/alpha/decisions'
    assert seen[-1].url.scheme == 'https' and seen[-1].url.host == 'openrouter.ai'
    assert seen[-1].headers['authorization'] == 'Bearer k'
    assert json.loads(seen[-1].content)['model'] == 'typesafe/jev-1.13'
    assert out == {'intitule': {'score': 2.5, 'max': 3, 'confidence': 0.7}}
