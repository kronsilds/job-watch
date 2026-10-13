"""Hygiène des sessions HTTP et structure du dashboard.

Deux régressions invisibles sans navigateur y sont couvertes :

* les sessions SQLAlchemy n'étaient jamais fermées : au bout de quelques
  onglets le pool (5 + 10) se saturait et l'API tombait en ``TimeoutError`` ;
* un ``</div>`` orphelin fermait ``.profile-grid`` avant l'``<aside>`` : la
  carte « Importer le CV » sortait de la grille et le profil passait sur une
  seule colonne.
"""
from html.parser import HTMLParser
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job

TEMPLATE = Path(__file__).resolve().parents[1] / 'app' / 'templates' / 'dashboard.html'
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
             'link', 'meta', 'param', 'source', 'track', 'wbr'}


def client_with_temp_db(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    engine = create_engine('sqlite:///' + str(tmp_path / 'http.db'),
                           connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'SessionLocal', session_factory)
    d = session_factory()
    d.add(Job(external_id='h1', title='Analyste', company='Acme', score=6.0))
    d.commit()
    d.close()
    return TestClient(main.app), engine


def test_each_request_releases_its_session(tmp_path, monkeypatch):
    """40 requêtes d'affilée sans jamais dépasser le pool (5 + 10)."""
    client, engine = client_with_temp_db(tmp_path, monkeypatch)
    for _ in range(40):
        assert client.get('/api/jobs').status_code == 200
        assert client.get('/api/kanban').status_code == 200
        assert client.get('/api/applications').status_code == 200
    assert engine.pool.checkedout() == 0


def test_sse_stream_finishes_and_releases_its_session(tmp_path, monkeypatch):
    """Un flux SSE se termine par un événement `done` et libère sa session."""
    client, engine = client_with_temp_db(tmp_path, monkeypatch)
    with client.stream('POST', '/api/search-now/stream',
                       params={'source': 'demo', 'enrich_skills': 'false'}) as response:
        assert response.status_code == 200
        body = ''.join(response.iter_text())
    assert '"type": "done"' in body
    assert engine.pool.checkedout() == 0


class ProfileGridProbe(HTMLParser):
    """Relève les enfants directs de l'élément portant la classe `profile-grid`."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.grid_depth = None
        self.children = []

    def _enter(self, tag, attrs):
        if self.grid_depth is None and 'profile-grid' in (dict(attrs).get('class') or '').split():
            self.grid_depth = self.depth
        elif self.grid_depth is not None and self.depth == self.grid_depth + 1:
            data = dict(attrs)
            self.children.append((tag, data.get('id') or data.get('class') or ''))

    def handle_starttag(self, tag, attrs):
        self._enter(tag, attrs)
        if tag not in VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self._enter(tag, attrs)  # balise auto-fermée : n'affecte pas la profondeur

    def handle_endtag(self, tag):
        if tag not in VOID_TAGS:
            self.depth -= 1


def test_profile_tab_stays_on_two_columns():
    """Le formulaire et la carte CV sont frères dans `.profile-grid` (deux colonnes)."""
    probe = ProfileGridProbe()
    probe.feed(TEMPLATE.read_text(encoding='utf-8'))
    assert probe.grid_depth is not None, 'profile-grid introuvable dans le template'
    tags = [tag for tag, _ in probe.children]
    assert tags.count('form') == 1 and tags.count('aside') == 1
    assert tags.index('form') < tags.index('aside')
    aside = next(name for tag, name in probe.children if tag == 'aside')
    assert 'cv-card' in aside