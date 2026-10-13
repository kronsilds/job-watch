from app.applications import quality
from app.llm import DeterministicProvider
def test_provider_is_deterministic_and_fact_bound():
 f={'job_title':'Data Analyst','company':'Acme','matched_skills':['SQL']}
 x=DeterministicProvider().generate('',f); y=DeterministicProvider().generate('',f)
 assert x==y and 'SQL' in x['pitch'] and 'Python' not in x['pitch']
def test_quality_warns_missing_source_facts():
 class J: id=1; title='Data Analyst'; company=''; skills=[]; url=''
 class P: id=1; data={'skills':['SQL']}
 q=quality(J(),P(), 'pitch'); assert not q['ok'] and len(q['warnings'])>=2
