"""Endpoints streamés (SSE) : progression en temps réel des opérations longues.

Vérifie que les routes ``/stream`` émettent des événements de progression
puis un résultat final identique à celui des routes historiques, et que ces
dernières conservent exactement le même comportement après refactorisation.
"""
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base
from app.models import Profile


def _client(tmp_path, monkeypatch):
    engine = create_engine(
        'sqlite:///' + str(tmp_path / 'stream.db'),
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'db', lambda: SL())
    return TestClient(main.app)


def _frames(text):
    events = []
    for frame in text.split('\n\n'):
        for line in frame.split('\n'):
            if line.startswith('data:'):
                events.append(json.loads(line[5:].strip()))
    return events


def _seed_profile():
    d = main.db()
    d.add(Profile(status='validated', data={'skills': ['sql', 'python'], 'jobs': ['data analyst']}))
    d.commit()


def test_cv_import_stream_emits_progress_then_result(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    cv = 'Camille Martin\nDéveloppeuse Full Stack\n6 ans en Python, JavaScript et React.'
    r = c.post('/api/cv/import/stream', files={'file': ('cv.txt', cv.encode('utf-8'), 'text/plain')})
    assert r.status_code == 200
    assert r.headers['content-type'].startswith('text/event-stream')
    events = _frames(r.text)
    steps = [e['step'] for e in events if e['type'] == 'progress']
    assert steps[0] == 'received' and 'extract' in steps and 'extract_done' in steps
    assert events[-1]['type'] == 'done'
    result = events[-1]['result']
    assert result['data']['name'] == 'Camille Martin'
    assert 'python' in [s.lower() for s in result['data']['skills']]
    assert result['analysis']['review_required'] is True
    assert 'note' in result


def test_cv_import_stream_rejects_bad_format(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post('/api/cv/import/stream', files={'file': ('virus.exe', b'x', 'application/octet-stream')})
    assert r.status_code == 400


def test_campaign_stream_emits_progress_and_result(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    _seed_profile()
    r = c.post('/api/campaign/run/stream', params={'source': 'demo'})
    assert r.status_code == 200
    assert r.headers['content-type'].startswith('text/event-stream')
    events = _frames(r.text)
    kinds = [e['type'] for e in events]
    assert kinds.count('progress') >= 3 and kinds[-1] == 'done'
    steps = [e['step'] for e in events if e['type'] == 'progress']
    assert 'imported' in steps and 'scoring' in steps and 'market' in steps
    result = events[-1]['result']
    assert result['imported'] >= 1 and result['source'] == 'demo'
    assert 'market' in result and 'market_summary' in result


def test_campaign_stream_requires_validated_profile(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post('/api/campaign/run/stream', params={'source': 'demo'})
    assert r.status_code == 200
    events = _frames(r.text)
    assert events[0]['type'] == 'error' and 'Profil non validé' in events[0]['message']


def test_sync_campaign_endpoint_keeps_same_shape(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    _seed_profile()
    r = c.post('/api/campaign/run', params={'source': 'demo'})
    assert r.status_code == 200
    body = r.json()
    for key in ('imported', 'processed', 'shortlisted', 'retained', 'source',
                'market', 'market_summary', 'market_analysis'):
        assert key in body


def test_sync_campaign_endpoint_keeps_http_error(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post('/api/campaign/run', params={'source': 'demo'})
    assert r.status_code == 400 and 'Profil non validé' in r.json()['detail']


def test_search_now_stream_demo(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post('/api/search-now/stream', params={'source': 'demo'})
    assert r.status_code == 200
    events = _frames(r.text)
    assert events[-1]['type'] == 'done'
    assert events[-1]['result']['imported'] >= 1
