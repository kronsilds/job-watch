"""Enrichissements : détails d'offres + filtre décision, génération
progressive avec CV ATS, et complément LLM léger à l'extraction heuristique."""
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base
from app.models import Profile, Job
from app.profile_analysis import (
    heuristic_analysis, complete_with_light, normalize, _sparse,
)


def _client(tmp_path, monkeypatch, provider='local'):
    monkeypatch.setenv('LLM_PROVIDER', provider)
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    engine = create_engine(
        'sqlite:///' + str(tmp_path / 'enh.db'),
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'db', lambda: SL())
    return TestClient(main.app), SL


def _frames(text):
    events = []
    for frame in text.split('\n\n'):
        for line in frame.split('\n'):
            if line.startswith('data:'):
                events.append(json.loads(line[5:].strip()))
    return events


def _seed_validated(sl, jobs=()):
    d = sl()
    d.add(Profile(status='validated',
                  data={'skills': ['SQL', 'Python'], 'jobs': ['Data Analyst'],
                        'main_job': 'Data Analyst', 'profile_summary': 'Analyste BI'}))
    for kw in jobs:
        d.add(Job(**kw))
    d.commit()


def test_jobs_expose_details_and_decision_filter(tmp_path, monkeypatch):
    c, sl = _client(tmp_path, monkeypatch)
    _seed_validated(sl, jobs=[
        dict(external_id='a', title='Data Analyst BI', company='Acme', score=8.0,
             decision='new', salary_min=45000, salary_max=55000, location='Lyon',
             skills=['SQL', 'Python'], sectors=['data'], url='https://acme/j1',
             description='BI dashboards'),
        dict(external_id='b', title='Marketing', company='BZ', score=3.0, decision='ignored'),
        dict(external_id='c', title='Retained', company='C', score=7.0, decision='retained',
             skills=['SQL']),
    ])
    all_jobs = c.get('/api/jobs').json()
    assert [j['decision'] for j in all_jobs] == ['new', 'retained', 'ignored']
    for key in ('skills', 'sectors', 'salary_min', 'salary_max', 'url', 'description'):
        assert key in all_jobs[0]
    assert [j['id'] for j in c.get('/api/jobs', params={'decision': 'ignored'}).json()] == [2]
    assert [j['id'] for j in c.get('/api/jobs', params={'decision': 'retained'}).json()] == [3]
    assert c.get('/api/jobs', params={'decision': 'new'}).json()[0]['title'] == 'Data Analyst BI'


def test_candidater_stream_creates_and_generates_ats_cv(tmp_path, monkeypatch):
    c, sl = _client(tmp_path, monkeypatch)
    _seed_validated(sl, jobs=[
        dict(external_id='a', title='Data Analyst BI', company='Acme', score=8.0,
             decision='retained', skills=['SQL', 'Python'], description='etl SQL'),
    ])
    r = c.post('/api/applications/candidater/stream', params={'job_id': 1})
    assert r.status_code == 200
    events = _frames(r.text)
    steps = [e['step'] for e in events if e['type'] == 'progress']
    for want in ('create', 'pitch', 'cv', 'verify'):
        assert want in steps
    assert events[-1]['type'] == 'done'
    result = events[-1]['result']
    assert result['state'] == 'to_validate'
    assert 'cv_after' in result and 'CURRICULUM' in result['cv_after'].upper()
    assert result['skills_to_highlight'] and result['pitch']


def test_generate_stream_reuses_existing_application(tmp_path, monkeypatch):
    c, sl = _client(tmp_path, monkeypatch)
    _seed_validated(sl, jobs=[
        dict(external_id='a', title='Data Analyst', company='Acme', score=8.0,
             decision='retained', skills=['SQL']),
    ])
    app = c.post('/api/applications', params={'job_id': 1}).json()
    r = c.post(f"/api/applications/{app['id']}/generate/stream")
    assert r.status_code == 200
    result = _frames(r.text)[-1]['result']
    assert result['state'] == 'to_validate' and result['cv_after']


def test_heuristic_sparse_triggers_light_completion(tmp_path, monkeypatch):
    # En mode local la passe light retombe sur l'heuristique : le résultat ne
    # perd jamais la baseline et reste dans la structure attendue.
    c, sl = _client(tmp_path, monkeypatch)
    sparse = heuristic_analysis('Expérience: 2 ans. Excel.')
    assert _sparse(sparse)
    dd, meta = __import__('app.profile_analysis', fromlist=['analyze_cv']).analyze_cv(
        'Expérience: 2 ans. Excel, PowerPoint.')
    assert meta['provider'] == 'heuristic' and 'excel' in dd['skills']
    # Fusion manuelle : la baseline heuristique comble les champs du light.
    light = {'job': None, 'skills': ['PowerPoint'], 'locations': []}
    merged = normalize(complete_with_light(light, sparse, 'Expérience: 2 ans. Excel.'))
    assert sorted(merged['skills']) == sorted(['PowerPoint', 'excel'])
