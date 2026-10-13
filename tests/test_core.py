from pathlib import Path
from app.extract import extract_structured
from app.scoring import score_job
class J:
 title='Data Analyst BI';skills=['SQL','Python'];location='Lyon';remote='hybride';contract='CDI';salary_max=50000;description='dashboards'
def test_extract_does_not_invent():
 x=extract_structured('Expérience: 3 ans. SQL et Python à Lyon.');assert x['years_experience']==3;assert 'sql' in x['skills'];assert x['jobs']==[]
def test_mandatory_gate():
 s,c,e=score_job(J(),{'skills':['SQL','Python'],'jobs':['Data Analyst'],'locations':['Lyon'],'contracts':['CDI']});assert s==10.0 and e['mandatory']
def test_location_gate_caps_score():
 s,c,e=score_job(J(),{'skills':['SQL'],'jobs':['Data Analyst'],'locations':['Paris']});assert s<=3.9 and not e['mandatory']
