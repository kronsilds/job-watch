from app.scoring import score_job
from app.market import analyze_market
from app.db import Base


class J:
    def __init__(self, title='Data Analyst BI', skills=('SQL', 'Python'), location='Lyon',
                 remote='hybride', contract='CDI', salary_max=50000, description='dashboards'):
        self.title = title; self.skills = list(skills); self.location = location
        self.remote = remote; self.contract = contract; self.salary_max = salary_max
        self.description = description


def test_default_weights_match_previous_behavior():
    # Mandatory gate + full match => 10.0
    s, c, e = score_job(J(), {'skills': ['SQL', 'Python'], 'jobs': ['Data Analyst'],
                              'locations': ['Lyon'], 'contracts': ['CDI']})
    assert s == 10.0 and e['mandatory'] and e['weights']['competences'] <= 1.0


def test_missing_contract_is_not_capped():
    # La donnée "contrat" est absente de l'offre, pas en conflit avec le profil :
    # le filtre obligatoire n'est pas considéré comme échoué et le score n'est pas plafonné.
    job = J(contract='')
    s, _, e = score_job(job, {'skills': ['SQL', 'Python'], 'jobs': ['Data Analyst'],
                              'locations': ['Lyon'], 'contracts': ['CDI']})
    assert e['gates']['contrat'] is None
    assert e['mandatory'] is True
    assert s > 3.9


def test_missing_salary_is_neutral_not_credit():
    # Profil exigeant un salaire, mais offre sans salaire renseigné : neutre (ni bloquant
    # ni plein crédit), au lieu d'un crédit systématique.
    s, _, e = score_job(J(salary_max=None), {'skills': ['SQL', 'Python'], 'jobs': ['Data Analyst'],
                                             'locations': ['Lyon'], 'contracts': ['CDI'],
                                             'salary_min': 45000})
    assert e['gates']['salaire'] is None
    assert e['dimensions']['salaire'] == 5.0


def test_real_conflict_still_caps():
    # Un vrai conflit (localisation différente) reste bloquant et plafonne à 3.9.
    s, _, e = score_job(J(location='Paris'), {'skills': ['SQL', 'Python'], 'jobs': ['Data Analyst'],
                                              'locations': ['Lyon'], 'contracts': ['CDI']})
    assert e['gates']['localisation'] is False
    assert not e['mandatory']
    assert s <= 3.9


def test_custom_weights_from_profile_change_score():
    # Location missing in job, competences present: raise competences weight => higher score
    base = {'skills': ['SQL'], 'jobs': ['Data Analyst'], 'locations': ['Lyon'], 'contracts': ['CDI']}
    job = J(location='Paris')  # location mismatch (non mandatory for loc? it IS mandatory gate)
    p_comp = dict(base, ponderation={'competences': 1.0, 'intitule': 0.0,
                                     'localisation': 0.0, 'contrat': 0.0, 'salaire': 0.0})
    p_loc = dict(base, ponderation={'competences': 0.0, 'intitule': 0.0,
                                    'localisation': 1.0, 'contrat': 0.0, 'salaire': 0.0})
    s_comp, _, _ = score_job(job, p_comp)
    s_loc, _, _ = score_job(job, p_loc)
    # With competences dominant the score is higher than with location dominant (Paris != Lyon)
    assert s_comp > s_loc


def test_market_analyzer_heuristic_offline(monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'local')
    from pathlib import Path
    import tempfile
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Profile, Job
    e = create_engine('sqlite:///' + str(Path(tempfile.mkdtemp()) / 't.db'))
    Base.metadata.create_all(e)
    d = sessionmaker(bind=e)()
    p = Profile(status='validated', data={'metiers_vises': ['Data analyst'], 'seniorite': ['senior']})
    j1 = Job(external_id='a', title='Data Analyst', company='A', location='Lyon', skills=['SQL'])
    j2 = Job(external_id='b', title='Data Analyst', company='B', location='Paris', skills=['Python', 'SQL'])
    d.add_all([p, j1, j2]); d.commit()
    prof = {'metiers_vises': ['Data analyst'], 'seniorite': ['senior']}
    out = analyze_market(prof, d)
    assert out['facts']['jobs_count'] == 2
    assert out['facts']['skills'] and out['facts']['skills'][0][0].lower() in ('sql', 'python')