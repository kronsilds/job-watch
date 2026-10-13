"""Appariement des compétences profil ↔ offre, et son effet sur la note.

Le bug corrigé : la matrice du profil porte des libellés riches (« .NET Core
(8-10) », « API REST ») alors que l'index d'une offre est fait de mots-clés nus
(« dotnet », « rest »). L'ancien score comparait les chaînes à l'identique, donc
la dimension « Compétences » tombait à 0 alors que la compétence était présente
des deux côtés, et l'interface affichait les compétences *du profil* sous le
libellé « Compétences demandées absentes du profil ».
"""
from app.applications import facts_for, quality
from app.scoring import score_job
from app.skills import match_skills, normalize_skill, skill_matches


def test_normalize_strips_levels_accents_and_separators():
    assert normalize_skill('.NET Core (8-10)') == 'net core'
    assert normalize_skill('Anglais (C1)') == 'anglais'
    assert normalize_skill('Développement') == 'developpement'
    assert normalize_skill('Git / Git Flow') == 'git git flow'


def test_matches_profile_labels_with_bare_offer_keywords():
    for label, keyword in [
        ('.NET Core (8-10)', 'dotnet'),
        ('Agents IA (Claude)', 'ia'),
        ('API REST', 'rest'),
        ('SQL Server', 'sql'),
        ('Microsoft Azure', 'azure'),
        ('Git / Git Flow', 'git'),
        ('Node.js', 'nodejs'),
        ('React 19', 'react'),
        ('Anglais (C1)', 'anglais'),
        ('Architecture logicielle', 'architecture'),
        ('Agents IA (Claude)', 'Agentic AI'),
        ('Gestion de projet', 'project management'),
        ("Direction d'équipes", 'team management'),
        ('Microservices', 'microservice'),
    ]:
        assert skill_matches(label, keyword), (label, keyword)


def test_matches_never_confuses_neighbouring_technologies():
    assert not skill_matches('Java', 'JavaScript')
    assert not skill_matches('PostgreSQL', 'sql')
    assert not skill_matches('Direction technique', "Direction d'équipes")


def test_match_skills_reports_both_sides_without_double_counting():
    matched, matched_job, unmatched_profile, unmatched_job = match_skills(
        ['API REST', 'SQL Server', 'Python', 'Agile'],
        ['sql', 'python', 'rest', 'production', 'gcp'])
    assert matched == ['API REST', 'SQL Server', 'Python']
    assert sorted(matched_job) == ['python', 'rest', 'sql']
    assert unmatched_profile == ['Agile']
    assert unmatched_job == ['production', 'gcp']


class J:
    def __init__(self, **over):
        self.title = 'Tech Lead'
        self.skills = ['dotnet', 'rest', 'angular']
        self.location = 'Paris'
        self.remote = ''
        self.contract = ''
        self.salary_max = None
        self.description = ''
        for key, value in over.items():
            setattr(self, key, value)


def test_score_covers_the_offer_demand_not_the_profile_size():
    # Profil riche : 1 compétence sur 3 de l'offre est couverte -> 1/3 de la
    # demande, et non « 1 compétence / 40 au profil » comme avant.
    profile = {'skills': ['Rh', 'Marketing', 'Autre', 'Autre 2', '.NET Core (8-10)'],
               'jobs': ['Tech Lead']}
    _, _, e = score_job(J(), profile)
    assert e['dimensions']['competences'] == 3.3
    assert e['competences_source'] == 'offre'
    assert e['strengths'] == ['.NET Core (8-10)']


def test_score_is_neutral_when_the_offer_has_no_indexed_skill():
    profile = {'skills': ['SQL', 'Python'], 'jobs': ['Tech Lead']}
    _, _, e = score_job(J(skills=[]), profile)
    assert e['dimensions']['competences'] == 5.0
    assert e['competences_source'] == 'absente'
    # Aucune accusation infondée : l'offre ne demande rien de « manquant ».
    assert e['gaps'] == []


def test_gaps_are_offer_demands_missing_from_the_profile():
    profile = {'skills': ['SQL'], 'jobs': ['Tech Lead']}
    _, _, e = score_job(J(skills=['sql', 'kubernetes']), profile)
    assert e['gaps'] == ['kubernetes']
    assert e['profile_gaps'] == []


class JobRow:
    def __init__(self, skills):
        self.title = 'Tech Lead'
        self.company = 'Acme'
        self.url = 'https://acme/offre'
        self.skills = skills


class ProfileRow:
    def __init__(self, skills):
        self.id = 1
        self.data = {'skills': skills}


def test_facts_match_rich_profile_labels_and_keep_case():
    facts = facts_for(JobRow(['dotnet', 'rest']),
                      ProfileRow(['.NET Core (8-10)', 'API REST', 'Agile']))
    assert facts['matched_skills'] == ['.NET Core (8-10)', 'API REST']
    assert facts['unmatched_job_skills'] == []
    assert 'Agile' in facts['profile_skills']


def test_quality_only_claims_a_mismatch_when_the_offer_is_indexed():
    missing = quality(JobRow([]), ProfileRow(['SQL']), 'pitch')
    assert not any('Aucune compétence du profil ne correspond' in w
                   for w in missing['warnings'])
    mismatch = quality(JobRow(['kubernetes']), ProfileRow(['SQL']), 'pitch')
    assert any('Aucune compétence du profil ne correspond' in w
               for w in mismatch['warnings'])
