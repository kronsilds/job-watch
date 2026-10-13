from app.profile_analysis import heuristic_analysis, normalize


def test_profile_analysis_keeps_reviewable_skill_evidence():
    data = normalize({
        'skills': [],
        'skill_matrix': [{'skill': 'SQL', 'category': 'Data', 'level': 'opérationnel', 'evidence': 'Requêtes dans les projets'}],
        'jobs': ['Data analyst'],
    })
    assert data['skills'] == ['SQL']
    assert data['skill_matrix'][0]['evidence'] == 'Requêtes dans les projets'


def test_local_profile_analysis_is_safe_fallback():
    data = heuristic_analysis('3 ans de SQL et Python, télétravail.')
    assert 'sql' in data['skills']
    assert data['skill_matrix']


def test_heuristic_fills_name_and_primary_secondary_jobs():
    data = heuristic_analysis(
        "Camille Martin\nDéveloppeuse Full Stack\n6 ans en Python, JavaScript et React, télétravail à Lyon."
    )
    assert data['name'] == 'Camille Martin'
    assert data['main_job'].lower() == 'développeuse full stack'
    assert data['jobs'] and data['jobs'][0].lower() == 'développeuse full stack'


def test_heuristic_returns_empty_jobs_without_title():
    data = heuristic_analysis('Expérience: 3 ans. SQL et Python à Lyon.')
    assert data['jobs'] == [] and data['main_job'] == ''


def test_heuristic_generalist_keywords_any_profession():
    # Non-data professions must still be detected thanks to the broad lexicon.
    data = heuristic_analysis('Graphiste senior : Photoshop, Illustrator, UX, CI/CD du web.')
    assert any(s in data['skills'] for s in ('photoshop', 'illustrator'))
    assert data['jobs']


def test_normalize_derives_main_and_secondary_from_jobs():
    data = normalize({
        'jobs': ['Data analyst', 'BI analyst'],
        'main_job': '', 'secondary_job': '', 'skills': ['SQL'], 'skill_matrix': [],
    })
    assert data['main_job'] == 'Data analyst'
    assert data['secondary_job'] == 'BI analyst'
