"""Affinage du profil : source unique compétences/résumé, prompt IA et API.

Trois régressions de l'import CV sont couvertes ici :

* le résumé de profil n'était jamais généré (le schéma du prompt l'ignorait) ;
* ``skills`` dupliquait ``skill_matrix`` avec des libellés en minuscules
  (« sql » à côté de « SQL Server ») ;
* l'interface n'exposait ni secteurs, exclusions, salaires ni affinage IA.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base
from app.llm import DeterministicProvider
from app.profile_analysis import (
    build_profile_summary, heuristic_analysis, merge_skill_list, normalize,
    refine_with_prompt,
)

# Fiche issue d'une analyse LLM réelle : liste plate bruitée + matrice détaillée.
LLM_FICHE = {
    'name': 'Olivier ROUGERON',
    'main_job': 'Architecte Logiciel / Tech Lead',
    'jobs': ['Architecte Logiciel / Tech Lead', 'Directeur informatique'],
    'skills': ['sql', 'react', 'git', 'azure', 'rest', 'crm', 'r', 'Architecture logicielle'],
    'skill_matrix': [
        {'skill': 'SQL Server', 'category': 'Bases de données', 'level': 'confirmé',
         'evidence': 'Environnement technique SQL Server.'},
        {'skill': 'React 19', 'category': 'Frameworks', 'level': 'confirmé',
         'evidence': 'Environnement technique React 19.'},
        {'skill': 'Git / Git Flow', 'category': 'DevOps', 'level': 'confirmé',
         'evidence': 'Outils & cloud : Git / Git Flow.'},
        {'skill': 'Microsoft Azure', 'category': 'Cloud', 'level': 'confirmé',
         'evidence': 'Environnement technique Azure.'},
        {'skill': 'API REST', 'category': 'Architecture', 'level': 'confirmé',
         'evidence': 'Ouverture des API aux ETLs.'},
        {'skill': 'Architecture logicielle', 'category': 'Architecture', 'level': 'expert',
         'evidence': "25 ans d'architecture logicielle."},
    ],
}


def test_normalize_makes_skills_the_projection_of_the_matrix():
    data = normalize(LLM_FICHE)
    labels = [row['skill'] for row in data['skill_matrix']]
    # Chaque compétence détaillée est présente une seule fois, avec son libellé.
    assert all(label in data['skills'] for label in labels)
    assert data['skills'].count('Architecture logicielle') == 1
    # Les doublons courts disparaissent au profit du libellé de la matrice.
    lowered = [s.lower() for s in data['skills']]
    assert 'sql' not in lowered and 'react' not in lowered
    assert 'git' not in lowered and 'azure' not in lowered and 'rest' not in lowered
    # Une compétence réellement distincte est conservée.
    assert 'crm' in lowered


def test_merge_skill_list_keeps_distinct_short_languages():
    assert merge_skill_list(['Java', 'sql', 'R'], ['JavaScript', 'SQL Server']) == \
        ['Java', 'R', 'JavaScript', 'SQL Server']


def test_normalize_generates_a_factual_profile_summary():
    data = normalize({
        'main_job': 'Architecte logiciel', 'secondary_job': 'Tech Lead',
        'years_experience': 25, 'skills': ['Architecture logicielle'],
        'locations': ['Paris'], 'remote': ['remote'],
    })
    assert data['profile_summary'].startswith('Architecte logiciel (Tech Lead)')
    assert "25 ans d'expérience" in data['profile_summary']
    assert 'Architecture logicielle' in data['profile_summary']
    assert 'Paris' in data['profile_summary']
    # Un résumé fourni par le modèle n'est jamais écrasé.
    assert normalize({**data, 'profile_summary': 'Résumé du modèle'})['profile_summary'] == \
        'Résumé du modèle'
    assert build_profile_summary({}) == ''


def test_heuristic_skills_are_the_flat_projection_of_the_matrix():
    data = heuristic_analysis('Data analyst : SQL, Python et React, télétravail à Lyon.')
    assert data['skills'] == [row['skill'] for row in data['skill_matrix']]
    assert data['profile_summary']


def test_single_letter_keywords_need_an_uppercase_occurrence():
    # « r » ne doit pas être détecté dans du texte courant…
    assert 'r' not in [s.lower() for s in heuristic_analysis('Développement react.')['skills']]
    # …mais le langage R reste détecté quand il est écrit tel quel.
class FakeRefineProvider:
    name = 'fake'
    model_used = 'fake-model'

    def __init__(self):
        self.calls = []

    def refine_profile(self, instruction, profile, cv_text=''):
        self.calls.append((instruction, profile, cv_text))
        return {
            'profile': {**profile, 'main_job': 'Tech Lead', 'jobs': ['Tech Lead'],
                        'skills': ['TypeScript'],
                        'skill_matrix': [{'skill': 'TypeScript', 'category': 'Langages',
                                          'level': 'expert', 'evidence': 'CV'}]},
            'changes_summary': 'Recentrage sur un poste de Tech Lead.',
        }


def test_refine_with_prompt_applies_the_model_fiche():
    provider = FakeRefineProvider()
    data, meta = refine_with_prompt(
        {'jobs': ['Architecte'], 'main_job': 'Architecte', 'skills': ['TypeScript']},
        'Recentre sur Tech Lead', 'CV : TypeScript, React.', provider=provider)
    assert meta['applied'] is True and meta['changes_summary']
    assert meta['provider'] == 'fake' and meta['warning'] == ''
    assert data['main_job'] == 'Tech Lead' and data['jobs'] == ['Tech Lead']
    assert provider.calls[0][2] == 'CV : TypeScript, React.'


def test_refine_without_model_keeps_the_profile_untouched():
    data, meta = refine_with_prompt(
        {'main_job': 'Data analyst', 'jobs': ['Data analyst'], 'skills': ['SQL']},
        'Ajoute du cloud', 'CV', provider=DeterministicProvider())
    assert meta['applied'] is False and 'Aucun modèle' in meta['warning']
    assert data['skills'] == ['SQL'] and data['jobs'] == ['Data analyst']
    assert data['profile_summary']  # le résumé reste toujours renseigné


def test_refine_with_prompt_requires_an_instruction():
    with pytest.raises(ValueError):
        refine_with_prompt({'jobs': ['Data analyst']}, '   ')


def _client(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'refine.db'),
                           connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'db', lambda: SL())
    return TestClient(main.app), SL


def test_put_profile_normalizes_skills_and_summary(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    r = c.put('/api/profile', json={
        'name': 'Olivier', 'validate_profile': True, 'source_text': 'CV',
        'data': {
            'main_job': 'Architecte logiciel',
            'skills': ['sql', 'TypeScript'],
            'skill_matrix': [{'skill': 'SQL Server', 'category': 'Bases de données',
                              'level': 'confirmé', 'evidence': 'ETL SQL'}],
            'sources': ['adzuna'], 'seniorite': ['lead'],
            'metiers_vises': ['Architecte'],
        },
    })
    assert r.status_code == 200
    data = r.json()['data']
    assert data['profile_summary']
    assert 'SQL Server' in data['skills']
    assert 'sql' not in [s.lower() for s in data['skills']]
    assert data['sources'] == ['adzuna'] and data['seniorite'] == ['lead']
    assert data['metiers_vises'] == ['Architecte']


def test_refine_endpoint_without_model_reports_the_warning(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'local')
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    c, _ = _client(tmp_path, monkeypatch)
    r = c.post('/api/profile/refine', json={
        'instruction': 'Ajoute du cloud', 'name': 'Camille',
        'data': {'main_job': 'Data analyst', 'jobs': ['Data analyst'], 'skills': ['SQL']},
        'source_text': 'CV : SQL, Python.',
    })
    assert r.status_code == 200
    body = r.json()
    assert body['applied'] is False and 'Aucun modèle' in body['warning']
    assert body['data']['main_job'] == 'Data analyst'
    assert c.post('/api/profile/refine',
                  json={'instruction': '  ', 'data': {}}).status_code == 400


def test_refine_endpoint_returns_the_refined_fiche(tmp_path, monkeypatch):
    monkeypatch.setattr('app.profile_analysis.get_provider', FakeRefineProvider)
    c, _ = _client(tmp_path, monkeypatch)
    r = c.post('/api/profile/refine', json={
        'instruction': 'Recentre sur Tech Lead',
        'data': {'main_job': 'Architecte', 'jobs': ['Architecte'], 'skills': ['TypeScript']},
    })
    assert r.status_code == 200
    body = r.json()
    assert body['applied'] is True
    assert body['changes_summary'].startswith('Recentrage')
    assert body['data']['main_job'] == 'Tech Lead'
    assert body['data']['skill_matrix'][0]['skill'] == 'TypeScript'
    assert 'R' in heuristic_analysis('Langages : R et Python.')['skills']