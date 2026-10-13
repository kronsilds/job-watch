from datetime import datetime,timezone,timedelta
from app.db import Base
from app.models import Profile,Job,Application,EmailDraft
from app.tracking import approve_and_send,create_followup,approve_followup,update_status
from app.providers.email import GmailProvider

def setup_db(tmp_path,monkeypatch):
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 e=create_engine('sqlite:///'+str(tmp_path/'t.db')); Base.metadata.create_all(e); return sessionmaker(bind=e)()
def seed(d):
 p=Profile(status='validated',data={'skills':['SQL']}); j=Job(external_id='x',title='Analyst',company='Acme',skills=['SQL']); d.add_all([p,j]);d.commit(); a=Application(job_id=j.id,profile_id=p.id,state='approved',status='approved',email_subject='Hi',email_body='Hello',cv_after='CV');d.add(a);d.commit();return a
def test_send_requires_approval_and_is_idempotent(tmp_path):
 d=setup_db(tmp_path,None); a=seed(d)
 e=approve_and_send(d,a.id,'mock','candidate@example.test','k1'); assert e.sent and e.attachment_path
 assert approve_and_send(d,a.id,'mock','candidate@example.test','k1').id==e.id
 a.state='draft'; d.commit()
 try: approve_and_send(d,a.id,'mock','candidate@example.test','k2'); assert False
 except ValueError: pass
def test_followup_requires_approval(tmp_path):
 d=setup_db(tmp_path,None); a=seed(d); create_followup(d,a.id,datetime.now(timezone.utc)+timedelta(days=1)); assert a.followup_state=='pending_approval'; approve_followup(d,a.id); assert a.followup_state=='approved'
def test_oauth_never_embeds_secret(monkeypatch):
 monkeypatch.setenv('JOBWATCH_GMAIL_CLIENT_ID','public-client-id'); u=GmailProvider().authorization_url('s'); assert 'client_secret' not in u and 'client_id' in u
def test_upsert_jobs_enriches_skills(tmp_path):
 from app.sources import upsert_jobs
 from app.llm import DeterministicProvider
 d=setup_db(tmp_path,None)
 n=upsert_jobs(d,[{'external_id':'e1','title':'Développeur Python/React','company':'Acme','description':''}],provider=DeterministicProvider())
 j=d.query(Job).filter_by(external_id='e1').first()
 assert n==1 and 'python' in j.skills and 'react' in j.skills
def test_upsert_jobs_fills_missing_fields_via_llm_light(tmp_path):
 # Les champs non scrapés sont complétés par la passe LLM light (provider factice).
 from app.sources import upsert_jobs
 class FakeProvider:
  name='fake'
  def enrich_offer(self, offer):
   return {'contract':'cdi','location':'Lyon','skills':['python','pytorch'],'salary_max':60000}
 d=setup_db(tmp_path,None)
 n=upsert_jobs(d,[{'external_id':'e2','title':'ML Engineer','company':'Acme','description':'','skills':[]}],provider=FakeProvider())
 j=d.query(Job).filter_by(external_id='e2').first()
 assert n==1 and j.contract=='cdi' and j.location=='Lyon' and j.salary_max==60000
 assert 'pytorch' in j.skills

def test_kanban_endpoint_and_status_transitions(tmp_path,monkeypatch):
 """Le payload Kanban expose colonnes ordonnées + cartes enrichies (offre, relances),
 et le changement de statut est auditable avec refus des statuts inconnus."""
 from fastapi.testclient import TestClient
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 from app import main
 e=create_engine('sqlite:///'+str(tmp_path/'k.db'),connect_args={'check_same_thread':False})
 Base.metadata.create_all(e)
 SL=sessionmaker(bind=e,expire_on_commit=False)
 monkeypatch.setattr(main,'db',lambda:SL())
 d=SL()
 j=Job(external_id='k1',title='Analyste',company='Acme',skills=['sql'])
 p=Profile(status='validated',data={'skills':['sql']})
 d.add_all([j,p]); d.commit()
 a=Application(job_id=j.id,profile_id=p.id,state='draft'); d.add(a); d.commit()
 client=TestClient(main.app)
 body=client.get('/api/kanban').json()
 assert body['columns'][0]=='draft' and 'submitted' in body['columns'] and 'closed' in body['columns']
 card=next(c for c in body['cards'] if c['id']==a.id)
 assert card['job_title']=='Analyste' and card['company']=='Acme'
 assert card['status']=='draft' and card['followup_state']=='none'
 r=client.post(f'/api/applications/{a.id}/status?status=screening')
 assert r.status_code==200 and r.json()=={'id':a.id,'status':'screening'}
 body=client.get('/api/kanban').json()
 assert next(c for c in body['cards'] if c['id']==a.id)['status']=='screening'
 assert client.post(f'/api/applications/{a.id}/status?status=inexistant').status_code==400
 rep=client.get('/api/report').json()
 assert rep['total']>=1 and rep['by_status'].get('screening')==1
 assert client.post(f'/api/applications/{a.id}/followup').status_code==200
 assert client.post(f'/api/applications/{a.id}/followup/approve').status_code==200
 body=client.get('/api/kanban').json()
 assert next(c for c in body['cards'] if c['id']==a.id)['followup_state']=='approved'

def test_app_payload_exposes_tracking_fields():
 """Le dossier expose son statut Kanban et ses informations de relance."""
 from app.applications import app_payload
 a=Application(id=7,job_id=1,profile_id=1,state='approved',status='screening',followup_state='approved',followup_due_at=datetime(2026,1,1,tzinfo=timezone.utc),submitted_at=datetime(2026,1,1,tzinfo=timezone.utc))
 p=app_payload(a)
 assert p['status']=='screening' and p['followup_state']=='approved'
 assert p['followup_due_at'].startswith('2026-01-01') and p['submitted_at'].startswith('2026-01-01')

def test_job_payload_exposes_created_at():
 """La date d'ajout de l'annonce est exposée au front (annonce + dossier)."""
 from app.applications import job_payload
 j=Job(id=3,external_id='x',title='Analyste',created_at=datetime(2026,2,3,4,5,6,tzinfo=timezone.utc))
 assert job_payload(j)['created_at'].startswith('2026-02-03')
 assert job_payload(None) is None

def test_app_payload_carries_history():
 """Le dossier transporte sa chronologie datée fournie par l'appelant."""
 from app.applications import app_payload
 entry={'kind':'status_changed','at':'2026-03-04T05:06:07','label':'Statut : Entretien','status':'interview','state':''}
 source=[entry]
 p=app_payload(Application(id=1,job_id=2,profile_id=1), history=source)
 assert p['history']==[entry] and p['history'] is not source
 assert app_payload(Application(id=1,job_id=2,profile_id=1))['history']==[]

def test_status_history_labels_statuses_in_order(tmp_path):
 """La chronologie d'un dossier est datée, ordonnée et libellée en français."""
 from app.applications import create_application
 from app.tracking import status_history,history_index
 d=setup_db(tmp_path,None)
 p=Profile(status='validated',data={'skills':['SQL']}); j=Job(external_id='h1',title='Analyste',company='Acme',skills=['SQL'])
 d.add_all([p,j]); d.commit(); a=create_application(d,j.id)
 update_status(d,a.id,'submitted'); update_status(d,a.id,'screening')
 create_followup(d,a.id,datetime.now(timezone.utc)+timedelta(days=1)); approve_followup(d,a.id)
 history=status_history(d,a.id)
 kinds=[h['kind'] for h in history]
 assert kinds[0]=='application_created' and kinds[1]=='status_changed'
 labels=[h['label'] for h in history]
 assert 'Dossier créé' in labels
 assert 'Statut : Envoyée' in labels and 'Statut : Présélection' in labels
 assert 'Relance planifiée' in labels and 'Relance approuvée' in labels
 assert all(h['at'] for h in history)
 assert [h['status'] for h in history if h['kind']=='status_changed']==['submitted','screening']
 # index groupé : une requête pour tous les dossiers, filtrée par identifiant
 assert set(history_index(d))=={a.id} and status_history(d,999)==[]

def test_kanban_and_jobs_expose_dates(tmp_path,monkeypatch):
 """Le Kanban expose la date d'annonce, de création du dossier et de dernier mouvement,
 et /api/jobs la date d'ajout de l'annonce (colonnes « dates » de l'UI)."""
 from fastapi.testclient import TestClient
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 from app import main
 e=create_engine('sqlite:///'+str(tmp_path/'dates.db'),connect_args={'check_same_thread':False})
 Base.metadata.create_all(e)
 SL=sessionmaker(bind=e,expire_on_commit=False)
 monkeypatch.setattr(main,'db',lambda:SL())
 d=SL()
 j=Job(external_id='d1',title='Analyste',company='Acme',score=7.5,created_at=datetime(2026,4,5,6,7,8,tzinfo=timezone.utc))
 p=Profile(status='validated',data={'skills':['sql']})
 d.add_all([j,p]); d.commit()
 from app.applications import create_application
 a=create_application(d,j.id)
 client=TestClient(main.app)
 client.post(f'/api/applications/{a.id}/status?status=interview')
 card=next(c for c in client.get('/api/kanban').json()['cards'] if c['id']==a.id)
 assert card['job_created_at'].startswith('2026-04-05')
 assert card['application_created_at'] and card['status_updated_at']
 assert card['status_updated_at']>card['application_created_at']
 job=next(x for x in client.get('/api/jobs').json() if x['id']==j.id)
 assert job['created_at'].startswith('2026-04-05')
 dossier=client.get(f'/api/applications/{a.id}').json()
 assert [h['kind'] for h in dossier['history']][:2]==['application_created','status_changed']
 assert dossier['job']['created_at'].startswith('2026-04-05')

def test_jobs_created_at_migration_on_existing_database(tmp_path,monkeypatch):
 """La migration ajoute `jobs.created_at` à une base existante et le remplit
 (date de la première campagne) sans perdre les offres déjà importées."""
 from sqlalchemy import create_engine,inspect,text
 from app import db as dbmod
 engine=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
 with engine.begin() as conn:
  conn.execute(text('CREATE TABLE jobs (id INTEGER PRIMARY KEY, external_id VARCHAR, title VARCHAR)'))
  conn.execute(text('CREATE TABLE events (id INTEGER PRIMARY KEY, kind VARCHAR, created_at DATETIME)'))
  conn.execute(text("INSERT INTO jobs (id,external_id,title) VALUES (1,'x','Analyste')"))
  conn.execute(text("INSERT INTO events (id,kind,created_at) VALUES (1,'campaign','2026-01-02 03:04:05')"))
 monkeypatch.setattr(dbmod,'engine',engine)
 dbmod._add_missing_columns()
 assert 'created_at' in {c['name'] for c in inspect(engine).get_columns('jobs')}
 with engine.connect() as conn:
  value=conn.execute(text('SELECT created_at FROM jobs WHERE id=1')).scalar()
 assert str(value).startswith('2026-01-02')
 # idempotence : un second appel ne casse rien
 dbmod._add_missing_columns()

def test_overview_exposes_to_validate_count(tmp_path,monkeypatch):
 """Le hub « Suivi » s'appuie sur /api/overview : le compte de dossiers « à valider »
 alimente le bandeau « à traiter » (avec les offres à examiner et les relances dues)."""
 from fastapi.testclient import TestClient
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 from app import main
 e=create_engine('sqlite:///'+str(tmp_path/'ov.db'),connect_args={'check_same_thread':False})
 Base.metadata.create_all(e)
 SL=sessionmaker(bind=e,expire_on_commit=False)
 monkeypatch.setattr(main,'db',lambda:SL())
 d=SL()
 j=Job(external_id='o1',title='Analyste',company='Acme',skills=['sql'],score=8.0,decision='new')
 p=Profile(status='validated',data={'skills':['sql']})
 d.add_all([j,p]); d.commit()
 a1=Application(job_id=j.id,profile_id=p.id,state='to_validate')
 a2=Application(job_id=j.id,profile_id=p.id,state='approved')
 d.add_all([a1,a2]); d.commit()
 body=TestClient(main.app).get('/api/overview').json()
 assert body['to_validate_count']==1
 assert body['high_score_count']==1
 assert body['applications_count']==2
