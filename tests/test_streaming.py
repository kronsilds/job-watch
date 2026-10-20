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


def test_profile_refine_stream_emits_progress_then_result(tmp_path, monkeypatch):
    from tests.test_profile_refinement import FakeRefineProvider
    monkeypatch.setattr('app.profile_analysis.get_provider', FakeRefineProvider)
    c = _client(tmp_path, monkeypatch)
    body = {
        'instruction': 'Recentre sur Tech Lead',
        'data': {'main_job': 'Architecte', 'jobs': ['Architecte'],
                 'skills': ['TypeScript']},
        'source_text': 'CV : TypeScript, React.',
    }
    r = c.post('/api/profile/refine/stream', json=body)
    assert r.status_code == 200
    assert r.headers['content-type'].startswith('text/event-stream')
    events = _frames(r.text)
    steps = [e['step'] for e in events if e['type'] == 'progress']
    assert steps[:2] == ['prepare', 'model']
    assert 'apply' in steps and 'summary' in steps
    assert events[-1]['type'] == 'done'
    result = events[-1]['result']
    for key in ('data', 'analysis', 'applied', 'changes_summary', 'warning'):
        assert key in result
    assert result['applied'] is True
    assert result['data']['main_job'] == 'Tech Lead'
    assert result['changes_summary'].startswith('Recentrage')
    # La route synchrone renvoie exactement la même charge utile finale.
    assert c.post('/api/profile/refine', json=body).json() == result


def test_profile_refine_stream_without_model_reports_the_warning(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'local')
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    c = _client(tmp_path, monkeypatch)
    r = c.post('/api/profile/refine/stream', json={
        'instruction': 'Ajoute du cloud', 'data': {'main_job': 'Data analyst'}})
    assert r.status_code == 200
    events = _frames(r.text)
    assert events[-1]['type'] == 'done'
    result = events[-1]['result']
    assert result['applied'] is False and 'Aucun modèle' in result['warning']


def test_profile_refine_stream_rejects_empty_instruction(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post('/api/profile/refine/stream', json={'instruction': '  ', 'data': {}})
    assert r.status_code == 200
    events = _frames(r.text)
    assert events[-1]['type'] == 'error' and 'Consigne' in events[-1]['message']


def test_search_now_stream_shows_url_and_request_steps(tmp_path, monkeypatch):
    """Le flux veille détaille la génération d'URLs puis chaque appel réseau."""
    from app import websearch
    from app.scrapers import DEFAULT_SOURCES
    from tests.test_websearch import _fake_geo
    monkeypatch.setenv('JOBWATCH_WEB_CACHE_DIR', str(tmp_path / 'cache'))
    for var in ('FRANCE_TRAVAIL_TOKEN', 'ADZUNA_APP_ID', 'ADZUNA_APP_KEY'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(websearch, 'fetch_page', _fake_geo)
    c = _client(tmp_path, monkeypatch)
    d = main.db()
    d.add(Profile(status='validated', data={'metiers_vises': ['Data analyst'],
                                            'localisation': 'Paris'}))
    d.commit()
    r = c.post('/api/search-now/stream', params={'source': 'web'})
    assert r.status_code == 200
    events = _frames(r.text)
    steps = [e['step'] for e in events if e['type'] == 'progress']
    assert steps[0] == 'start'
    assert 'urls_start' in steps and 'urls_ready' in steps
    ready = next(e for e in events if e.get('step') == 'urls_ready')
    starts = [e for e in events if e.get('step') == 'request_start']
    # 1 métier × 1 zone × les sources HTML par défaut = autant d'URLs que d'appels.
    assert ready['requests'] == len(starts) == len(DEFAULT_SOURCES)
    for i, e in enumerate(starts, 1):
        assert e['url'] and e['index'] == i and e['total'] == len(starts)
    finished = [e for e in events
                if e.get('step') in ('request_done', 'request_error')]
    assert len(finished) == len(starts)
    assert events[-1]['type'] == 'done'
