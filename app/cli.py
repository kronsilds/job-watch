import argparse
from pathlib import Path
from .db import init_db,SessionLocal
from .config import DEMO_PATH
from .sources import read_records,upsert_jobs
from .scrapers import sources_for, fetch as scrape_fetch
from .models import Profile,Event,Application,Job
from .scoring import score_all
from .market import analyze, summarize, analyze_market as market_analyzer
from .applications import generate,transition,create_email_draft

def _records_for(d, source, file):
    if file:
        return read_records(Path(file))
    p=d.query(Profile).filter_by(status='validated').first()
    pdata=p.data if p else {}
    if source in ('web','auto') or source in sources_for(pdata):
        records,_=scrape_fetch(pdata) if source in ('web','auto') else scrape_fetch(pdata,sources=[source])
        return records
    return read_records(DEMO_PATH) if source=='demo' else read_records(Path(source))

def main():
 a=argparse.ArgumentParser();a.add_argument('command',choices=['init','search-now','campaign-run','market-analyze','dashboard','application-generate','application-state','email-draft','application-refine','application-revise','user-create','cache-list','cache-replay']);a.add_argument('id',type=int,nargs='?');a.add_argument('--file');a.add_argument('--state');a.add_argument('--source',default='demo');a.add_argument('--doc',default='pitch');a.add_argument('--comments',default='');a.add_argument('--user',default='');a.add_argument('--password',default='');a.add_argument('--limit',type=int,default=200);a.add_argument('--import-missing',action='store_true');x=a.parse_args();init_db();d=SessionLocal()
 if x.command=='init': print('Base initialisée.')
 elif x.command=='user-create':
  from .auth import normalize_username,hash_password
  from .models import User
  try:username=normalize_username(x.user)
  except ValueError as e:raise SystemExit(str(e))
  if d.query(User).filter_by(username=username).first():raise SystemExit('Nom déjà pris.')
  try:ph=hash_password(x.password)
  except ValueError as e:raise SystemExit(str(e))
  d.add(User(username=username,password_hash=ph));d.commit();print(f'Compte {username} créé.')
 elif x.command=='search-now':
  prof=d.query(Profile).filter_by(status='validated').first();pdata=prof.data if prof else None
  n=upsert_jobs(d,_records_for(d,x.source,x.file),profile_data=pdata);p=d.query(Profile).filter_by(status='validated').first()
  if p: score_all(d,p.data)
  d.add(Event(kind='cycle',message=f'{n} offre(s) importée(s)',metadata_json={'source':x.source}));d.commit();print(f'{n} offre(s) importée(s) (source: {x.source}).')
 elif x.command=='campaign-run':
  p=d.query(Profile).filter_by(status='validated').first()
  if not p: raise SystemExit('Validez le profil avant de lancer une campagne.')
  n=upsert_jobs(d,_records_for(d,x.source,x.file),profile_data=p.data);score_all(d,p.data);market=analyze(d);shortlisted=d.query(Job).filter(Job.score>=7,Job.decision=='new').count();analysis=market_analyzer(p.data,d);d.add(Event(kind='campaign',message='Campagne terminee',metadata_json={'source':x.source}));d.commit();print({'imported':n,'shortlisted':shortlisted,'market':market,'market_analysis':analysis})
 elif x.command=='market-analyze': print(market_analyzer((d.query(Profile).filter_by(status='validated').first().data if d.query(Profile).filter_by(status='validated').first() else {}),d))
 elif x.command=='dashboard': print('Lancer: uvicorn app.main:app --reload')
 elif x.command=='application-generate': print({'id':generate(d,x.id).id,'state':'to_validate'})
 elif x.command=='application-state': print({'id':transition(d,x.id,x.state).id,'state':x.state})
 elif x.command=='email-draft': print({'id':create_email_draft(d,x.id).id,'sent':False})
 elif x.command=='application-refine':
  from .refinement import run_refinement,refine_payload
  print(refine_payload(run_refinement(d,x.id,x.doc)))
 elif x.command=='application-revise':
  from .refinement import revise_with_user_comments,refine_payload
  print(refine_payload(revise_with_user_comments(d,x.id,x.comments,x.doc)))
 elif x.command=='cache-list':
  from .cache_debug import list_entries
  import json as _json
  print(_json.dumps(list_entries(limit=x.limit,source=x.source if x.source!='demo' else None),indent=2,ensure_ascii=False))
 elif x.command=='cache-replay':
  from .cache_debug import replay_all
  import json as _json
  res=replay_all(limit=x.limit,source=x.source if x.source!='demo' else None,db=d)
  n0=res.pop('offers');diags=res.pop('diagnostics')
  if x.import_missing and res.get('missing'):
   p=d.query(Profile).filter_by(status='validated').first()
   res['imported']=upsert_jobs(d,res.get('missing') or [],profile_data=(p.data if p else None))
  print(_json.dumps({**res,'missing_sample':res.get('missing') or []},indent=2,ensure_ascii=False)[:6000])
if __name__=='__main__': main()
