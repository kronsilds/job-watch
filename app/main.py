from pathlib import Path
import tempfile
from fastapi import FastAPI,Request,UploadFile,File,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse,StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from .db import init_db,SessionLocal
from .models import Profile,Job,Event,User,AuthToken,JobRating
from .auth import auth_enabled, hash_password, verify_password, new_token, normalize_username
from .user_jobs import ratings_map, overlay_job, get_or_create_rating
from .schemas import ProfileUpdate, ProfileRefinePrompt, OfferUrlImport, AuthCredentials, CacheReplayRequest
from .extract import text_from_file
from .manual import extract_url, fetch_offer, store_offer, source_label
from .profile_analysis import analyze_cv, normalize, refine_with_prompt
from .sources import read_records,upsert_jobs
from .scrapers import sources_for, fetch_iter, enrich_skills_from_text, MATRIX_CONNECTORS
from .progress import sse,P
from .scoring import score_all
from .market import analyze,summarize,analyze_market as market_analyzer
from .config import DEMO_PATH
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBearer
app=FastAPI(title='Job Watch — veille emploi',description='Veille emploi explicable, sans scraping.\n\n## Authentification (Bearer)\n\n1. Créez un compte : `POST /api/auth/register` avec `{"username": "alice", "password": "secret123"}`.\n2. Récupérez le `token` de la réponse (ou via `POST /api/auth/login`).\n3. Cliquez sur **Authorize** (cadenas en haut à droite), collez `VOTRE_TOKEN` (sans `Bearer `, Swagger l\u2019ajoute).\n4. Toutes les routes verrouillées (🔒) envoient alors `Authorization: Bearer <token>`.',version='3')
bearer_scheme=HTTPBearer(auto_error=False,description='Collez le token opaque retourné par /api/auth/register ou /api/auth/login (sans le préfixe « Bearer »).')
_PUBLIC_OPENAPI_PATHS={'/api/health','/api/auth/register','/api/auth/login','/','/api/oauth/{provider}/authorize','/api/oauth/claude/authorize','/api/oauth/claude/status','/api/oauth/claude/logout','/api/oauth/callback','/callback'}
def custom_openapi():
 if app.openapi_schema:return app.openapi_schema
 schema=get_openapi(title=app.title,version=app.version,description=app.description,routes=app.routes)
 schema.setdefault('components',{}).setdefault('securitySchemes',{})['BearerAuth']={'type':'http','scheme':'bearer','bearerFormat':'opaque','description':'Token opaque retourné par POST /api/auth/register ou POST /api/auth/login. Dans Swagger : Authorize → collez le token seul.'}
 for path, methods in (schema.get('paths') or {}).items():
  if path in _PUBLIC_OPENAPI_PATHS:continue
  for op in (methods or {}).values():
   if not isinstance(op,dict):continue
   op.setdefault('security',[{'BearerAuth':[]}])
 app.openapi_schema=schema;return app.openapi_schema
app.openapi=custom_openapi  # type: ignore[method-assign]
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static'); templates=Jinja2Templates(directory=Path(__file__).parent/'templates')
@app.on_event('startup')
def startup():init_db()
# Sessions SQLAlchemy : une par requête, fermées explicitement à la fin de la
# requête (ou du flux SSE). Sans cela les connexions restent retenues et le pool
# finit par se saturer après quelques onglets. La liste est mutée (append/pop),
# donc partagée par tous les contextes copiés : un générateur SSE qui tourne
# dans un thread est bien vu par le middleware.
from contextvars import ContextVar
_request_sessions:ContextVar[list]=ContextVar('jobwatch_request_sessions')
def db():
 s=SessionLocal()
 try:_request_sessions.get().append(s)
 except LookupError:pass  # hors requête HTTP (CLI, tests) : à l'appelant de fermer
 return s
def close_request_sessions():
 """Ferme les sessions ouvertes pendant la requête/flux courant (idempotent)."""
 try:sessions=_request_sessions.get()
 except LookupError:return
 while sessions:
  try:sessions.pop().close()
  except Exception:pass
@app.middleware('http')
async def release_request_sessions(request:Request,call_next):
 _request_sessions.set([])
 response=await call_next(request)
 # Un flux SSE s'itère après ce middleware : c'est _stream qui ferme ses sessions.
 if 'text/event-stream' not in (response.headers.get('content-type') or ''):close_request_sessions()
 return response
def upload_path(filename:str, content:bytes)->Path:
 suffix=Path(filename or '').suffix.lower()
 with tempfile.NamedTemporaryFile(suffix=suffix,delete=False) as tmp:
  tmp.write(content);return Path(tmp.name)
def _stream(events):
 def frames():
  try:
   for e in events:yield sse(e)
  finally:close_request_sessions()
 return StreamingResponse(frames(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
def _safe(events):
 try:yield from events
 except Exception as exc:yield P.error(str(exc))
def _consume(events):
 result=None
 for e in events:
  if e.get('type')=='error':raise HTTPException(400,e['message'])
  if e.get('type')=='done':result=e['result']
 return result
@app.get('/api/health')
def health():return {'status':'ok','version':'3','auth':auth_enabled()}
def current_user(request:Request, d=None, required=True):
 """Utilisateur courant via ``Authorization: Bearer <token>``.

 En mode ``JOBWATCH_SINGLE_USER=1`` (ou sans token et auth désactivée),
 renvoie ``None`` : les routes retombent sur les données historiques
 ``user_id IS NULL``.
 """
 if not auth_enabled():return None
 auth=request.headers.get('authorization','') if request is not None else ''
 token=auth[7:] if auth.lower().startswith('bearer ') else ''
 if not token:
  if required:raise HTTPException(401,'Authentification requise (Bearer token).')
  return None
 own=d is None
 if own:d=db()
 try:
  row=d.query(AuthToken).filter_by(token=token).first()
  if not row:
   if required:raise HTTPException(401,'Token invalide.')
   return None
  u=d.get(User,row.user_id)
  if not u and required:raise HTTPException(401,'Compte introuvable.')
  return u
 finally:
  pass
def _uid(user):return user.id if user is not None else None
@app.post('/api/auth/register',summary='Créer un compte (token Bearer retourné)',description='Crée un compte local et retourne un token opaque à coller dans Authorize. Corps : {"username": "alice", "password": "secret123"}.')
def register(credentials:AuthCredentials):
 d=db()
 try:username=normalize_username(credentials.username)
 except ValueError as e:raise HTTPException(400,str(e))
 if d.query(User).filter_by(username=username).first():raise HTTPException(409,'Nom déjà pris.')
 try:ph=hash_password(credentials.password)
 except ValueError as e:raise HTTPException(400,str(e))
 u=User(username=username,password_hash=ph);d.add(u);d.flush()
 tok=new_token();d.add(AuthToken(user_id=u.id,token=tok));d.commit()
 return {'id':u.id,'username':u.username,'token':tok}
@app.post('/api/auth/login',summary='Se connecter (token Bearer retourné)',description='Vérifie username/password et émet un nouveau token opaque (les anciens restent valides jusqu\u2019au logout). Corps : {"username": "alice", "password": "secret123"}.')
def login(credentials:AuthCredentials):
 d=db()
 try:username=normalize_username(credentials.username)
 except ValueError:raise HTTPException(401,'Identifiants invalides.')
 u=d.query(User).filter_by(username=username).first()
 if not u or not verify_password(credentials.password or '', u.password_hash or ''):
  raise HTTPException(401,'Identifiants invalides.')
 tok=new_token();d.add(AuthToken(user_id=u.id,token=tok));d.commit()
 return {'id':u.id,'username':u.username,'token':tok}
@app.post('/api/auth/logout')
def logout(request:Request):
 d=db();u=current_user(request,d)
 auth=request.headers.get('authorization','');tok=auth[7:] if auth.lower().startswith('bearer ') else ''
 if tok:d.query(AuthToken).filter_by(token=tok).delete();d.commit()
 return {'ok':True,'username':u.username if u else None}
@app.get('/api/auth/me')
def auth_me(request:Request):
 d=db();u=current_user(request,d,required=False)
 if not u:return {'user':None,'auth':auth_enabled()}
 return {'user':{'id':u.id,'username':u.username},'auth':auth_enabled()}
def _profile_query(d, uid):
 q=d.query(Profile)
 if uid is None and auth_enabled():return q.filter(Profile.user_id.is_(None))
 if uid is None:return q
 return q.filter_by(user_id=uid)
@app.get('/api/profile')
def get_profile(request:Request):
 d=db();u=current_user(request,d);p=_profile_query(d,_uid(u)).first()
 if not p:return {'name':'Candidat','status':'draft','data':{},'source_text':''}
 return {'id':p.id,'name':p.name,'status':p.status,'data':p.data or {},'source_text':p.source_text or ''}
@app.put('/api/profile')
def put_profile(payload:ProfileUpdate, request:Request):
 d=db();u=current_user(request,d);uid=_uid(u);p=_profile_query(d,uid).first() or Profile(user_id=uid);p.name=payload.name
 # Normalisation serveur : la matrice reste la source unique des compétences,
 # les compétences en doublon sont absorbées et un résumé est toujours généré.
 p.data=normalize(payload.data.model_dump());p.status='validated' if payload.validate_profile else 'draft';d.add(p);d.add(Event(user_id=uid,kind='profile',message='Profil validé' if payload.validate_profile else 'Profil enregistré'));d.commit();
 p.source_text=payload.source_text
 d.commit()
 if p.status=='validated':score_all(d,p.data,uid)
 return {'id':p.id,'status':p.status,'data':p.data}
@app.post('/api/profile/refine')
def refine_profile(payload:ProfileRefinePrompt, request:Request):
 """Affine la fiche selon une consigne libre (CV = seule base factuelle).

 Rien n'est enregistré : la fiche affinée est renvoyée, l'utilisateur la relit
 puis l'enregistre ou la valide (la validation reste un acte humain).
 """
 d=db();u=current_user(request,d);p=_profile_query(d,_uid(u)).first()
 source=(payload.source_text or (p.source_text if p else '') or '')
 try:
  data,meta=refine_with_prompt(payload.data.model_dump(),payload.instruction,source)
 except ValueError as e:raise HTTPException(400,str(e))
 except RuntimeError as e:raise HTTPException(502,str(e))
 return {'data':data,'analysis':meta,'applied':meta.get('applied',False),
         'changes_summary':meta.get('changes_summary') or '','warning':meta.get('warning') or ''}
CV_IMPORT_NOTE='Analyse à relire et compléter avant validation : nom, métier principal/secondaire et compétences ont été pré-remplis, aucune donnée ne valide automatiquement le profil.'
def _cv_import_steps(filename:str,content:bytes):
 yield P.progress('received',f'Fichier reçu : {filename or "CV"} ({max(1,len(content)//1024)} Ko)…',filename=filename or '',size=len(content))
 yield P.progress('extract','Extraction du texte du document…')
 text=text_from_file(upload_path(filename,content))
 yield P.progress('extract_done',f'Texte extrait : {len(text)} caractère(s). Analyse du CV (métiers, compétences, matrice)…')
 data,meta=analyze_cv(text)
 note=CV_IMPORT_NOTE
 if meta.get('provider')=='heuristic':
  note+=' Analyse locale (aucun modèle IA configuré) : le résumé du profil est déduit des faits détectés, complétez-le si besoin.'
 yield P.done({'source_text':text,'data':data,'analysis':meta,'note':note})
@app.post('/api/cv/import')
def import_cv(request:Request,file:UploadFile=File(...)):
 current_user(request,db())
 if Path(file.filename).suffix.lower() not in ('.pdf','.docx','.txt'):raise HTTPException(400,'Format accepté: PDF, DOCX ou TXT')
 return _consume(_cv_import_steps(file.filename,file.file.read()))
@app.post('/api/cv/import/stream')
def import_cv_stream(request:Request,file:UploadFile=File(...)):
 current_user(request,db())
 if Path(file.filename).suffix.lower() not in ('.pdf','.docx','.txt'):raise HTTPException(400,'Format accepté: PDF, DOCX ou TXT')
 content=file.file.read()
 return _stream(_safe(_cv_import_steps(file.filename,content)))
def _load_profile_data(d, uid=None):
 if uid is None and auth_enabled():
  p=d.query(Profile).filter(Profile.user_id.is_(None),Profile.status=='validated').first(); return p.data if p else None
 q=d.query(Profile).filter_by(status='validated')
 if uid is not None:q=q.filter_by(user_id=uid)
 p=q.first(); return p.data if p else None

def _fetch_with_progress(profile_data,source,wanted,with_enrich):
 names=sources_for(profile_data) if wanted is None else [source]
 yield P.progress('start',f'Interrogation de {len(names)} connecteur(s) : {", ".join(names)}…',source=source,sources=names)
 records=[];per_source={}
 # Matrice métier × zone × source (n8n) : un appel agrégé avec diagnostics
 # par requête ; repli connecteur-par-connecteur pour APEC/WTTJ (opt-in).
 from . import websearch as _websearch
 matrix_names=[n for n in names if n in MATRIX_CONNECTORS]
 try:
  if matrix_names:
   raws,detail=_websearch.scrape_matrix(profile_data,matrix_names)
   summary=detail.get('summary',{})
   for diag in detail.get('diagnostics',[]):
    st=diag.get('status');msg=f'{diag.get("source")} : {diag.get("query")} — {diag.get("location")} → {st} ({diag.get("kept",0)} offre(s))'
    if st in ('ok','no_results'):yield P.progress('request_done',msg,source=diag.get('source'),status=st,url=diag.get('url'))
    else:yield P.progress('request_error',msg,source=diag.get('source'),status=st,error=diag.get('message'),url=diag.get('url'))
   grouped={}
   for raw in raws:
    src=str(raw.get('_matrix_source') or 'web')
    clean={k:v for k,v in raw.items() if not k.startswith('_')}
    try:
     from .scrapers import normalize as _normalize_matrix
     clean=_normalize_matrix(clean,src)
    except Exception:clean.setdefault('_source',src)
    grouped.setdefault(src,[]).append(clean)
   for name in matrix_names:
    got=grouped.get(name,[])
    records+=got;per_source[name]={'count':len(got),'error':None}
    slot=(summary.get('sources') or {}).get(name,{})
    if not got and slot.get('requests'):
     bad={k:slot.get(k,0) for k in ('blocked','rate_limited','network_error','empty_response','parse_error')}
     if any(bad.values()):
      per_source[name]['error']='; '.join(f'{k}: {v}' for k,v in bad.items() if v)
      yield P.progress('source_error',f'{name} : échec — {per_source[name]["error"]}',source=name,error=per_source[name]['error']);continue
    yield P.progress('source_done',f'{name} : {len(got)} offre(s) récupérée(s).',source=name,count=len(got))
   if summary.get('warnings'):
    for w in summary['warnings']:yield P.progress('warning',w)
 except Exception as exc:
  for name in matrix_names:
   if name not in per_source:per_source[name]={'count':0,'error':str(exc)};yield P.progress('source_error',f'{name} : échec — {exc}',source=name,error=str(exc))
 legacy=[n for n in names if n not in MATRIX_CONNECTORS]
 if legacy:
  for name,got,err in fetch_iter(profile_data,sources=legacy):
   if err is not None:
    per_source[name]={'count':0,'error':err};yield P.progress('source_error',f'{name} : échec — {err}',source=name,error=err);continue
   # Tag transitoire de provenance AVANT dedup (permet le top-N par site).
   for r in got:
    try: r.setdefault('_source',name)
    except Exception: pass
   records+=got;per_source[name]={'count':len(got),'error':None}
   yield P.progress('source_done',f'{name} : {len(got)} offre(s).',source=name,count=len(got))
 unique={}
 for x in records:
  if x.get('external_id') and x['external_id'] not in unique: unique[x['external_id']]=x
 report={'sources':per_source,'total_raw':len(records),'total_unique':len(unique)}
 yield P.progress('dedupe',f'Déduplication : {report["total_raw"]} offre(s) brute(s) → {report["total_unique"]} unique(s).')
 records=list(unique.values())
 if with_enrich:
  yield P.progress('enrich','Enrichissement des compétences depuis les descriptions…')
  records=[enrich_skills_from_text(r) for r in records]
 return records,report
def _prescore_report(profile_data,records):
 """Pré-note gratuite (hors-ligne) pour le rapport : top-N pré-sélectionné par site."""
 try:
  from .sources import prescore_and_select,_top_n_per_source
  top_n=_top_n_per_source()
  selected,prescored=prescore_and_select(records,profile_data,top_n)
  by_src={}
  for r in prescored: by_src.setdefault(str(r.get('_source') or 'file'),[]).append(r)
  sel_ids={id(r) for r in selected}
  per_source={s:{'fetched':len(v),'selected':sum(1 for r in v if id(r) in sel_ids),'top_prescore':round(max((r.get('_prescore') or 0) for r in v),1) if v else 0.0} for s,v in by_src.items()}
  return {'top_n_per_source':top_n,'per_source':per_source,'selected':len(selected),'prescored':len(prescored)}
 except Exception: return None
def _search_now_steps(source:str,enrich_skills:bool, uid=None):
 d=db();profile_data=_load_profile_data(d,uid);report=None
 if source in ('web','auto') or source in sources_for(profile_data or {}):
  if not profile_data:
   yield P.error('Profil non validé : la veille web a besoin du profil (métiers cibles, sources).');return
  records,report=yield from _fetch_with_progress(profile_data,source,None if source in ('web','auto') else [source],enrich_skills)
  # Pré-note (gratuite) → top-5/site → infos manquantes (LLM) seulement sur le top.
  presel=_prescore_report(profile_data,records)
  if presel:
   report=dict(report or {});report['prescoring']=presel
   yield P.progress('prescore',f'Pré-notation : {presel["prescored"]} offre(s) notées, top-{presel["top_n_per_source"]} par site → {presel["selected"]} enrichie(s).',prescoring=presel)
  yield P.progress('enrich_llm','Complétion des champs manquants sur le top pré-noté (heuristique + LLM light si configuré)…')
  n=upsert_jobs(d,records,profile_data=profile_data);p=_profile_query(d,uid).filter_by(status='validated').first()
  yield P.progress('imported',f'{n} nouvelle(s) offre(s) importée(s) en base.')
  if p:
   yield P.progress('scoring',f'Note finale (scoring pondéré) de {d.query(Job).count()} offre(s)…')
   score_all(d,p.data,uid)
  d.add(Event(user_id=uid,kind='cycle',message=f'{n} offre(s) importée(s)',metadata_json={'source':source,'report':report}));d.commit()
  yield P.done({'imported':n,'scored':bool(p),'source':source,'report':report});return
 yield P.progress('file','Lecture du fichier de démonstration…' if source=='demo' else f'Lecture du fichier {source}…')
 n=upsert_jobs(d,read_records(DEMO_PATH) if source=='demo' else Path(source),profile_data=profile_data);p=_profile_query(d,uid).filter_by(status='validated').first()
 yield P.progress('imported',f'{n} nouvelle(s) offre(s) importée(s) en base.')
 if p:
  yield P.progress('scoring',f'Note finale (scoring pondéré) de {d.query(Job).count()} offre(s)…')
  score_all(d,p.data,uid)
 d.add(Event(user_id=uid,kind='cycle',message=f'{n} offre(s) importée(s)',metadata_json={'source':source}));d.commit()
 yield P.done({'imported':n,'scored':bool(p),'source':source,'report':report})
@app.post('/api/search-now')
def search_now(request:Request,source:str='demo',enrich_skills:bool=True):
 return _consume(_search_now_steps(source,enrich_skills,_uid(current_user(request,db()))))
@app.post('/api/search-now/stream')
def search_now_stream(request:Request,source:str='demo',enrich_skills:bool=True):
 uid=_uid(current_user(request,db()))
 def gen():yield from _search_now_steps(source,enrich_skills,uid)
 return _stream(_safe(gen()))
def _campaign_steps(source:str,enrich_skills:bool,analyze_market:bool, uid=None):
 d=db();profile_data=_load_profile_data(d,uid);report=None
 if not profile_data:
  yield P.error('Profil non validé : créez puis validez le profil (PUT /api/profile avec validate_profile=true) avant de lancer une campagne.');return
 if source in ('web','auto') or source in sources_for(profile_data):
  records,report=yield from _fetch_with_progress(profile_data,source,None if source in ('web','auto') else [source],enrich_skills)
  presel=_prescore_report(profile_data,records)
  if presel:
   report=dict(report or {});report['prescoring']=presel
   yield P.progress('prescore',f'Pré-notation : {presel["prescored"]} offre(s) notées, top-{presel["top_n_per_source"]} par site → {presel["selected"]} enrichie(s).',prescoring=presel)
  yield P.progress('enrich_llm','Complétion des champs manquants sur le top pré-noté (heuristique + LLM light si configuré)…')
  n=upsert_jobs(d,records,profile_data=profile_data)
 else:
  yield P.progress('file','Lecture du fichier de démonstration…' if source=='demo' else f'Lecture du fichier {source}…')
  n=upsert_jobs(d,read_records(DEMO_PATH) if source=='demo' else Path(source),profile_data=profile_data)
 yield P.progress('imported',f'{n} nouvelle(s) offre(s) importée(s) en base.')
 total=d.query(Job).count()
 yield P.progress('scoring',f'Note finale (scoring pondéré) de {total} offre(s)…')
 score_all(d,profile_data,uid)
 shortlisted=d.query(Job).filter(Job.score>=7,Job.decision=='new').count();retained=d.query(Job).filter_by(decision='retained').count()
 yield P.progress('market','Synthèse du marché…')
 summary=analyze(d);market_note=summarize(summary)
 analysis=None
 if analyze_market:
  yield P.progress('market_analysis','Analyse du marché (LLM)…')
  analysis=market_analyzer(profile_data,d)
 d.add(Event(user_id=uid,kind='campaign',message='Campaign completed',metadata_json={'source':source,'imported':n,'processed':total,'shortlisted':shortlisted,'retained':retained,'report':report}));d.commit()
 yield P.done({'imported':n,'processed':total,'shortlisted':shortlisted,'retained':retained,'source':source,'report':report,'market':summary,'market_summary':market_note,'market_analysis':analysis})
@app.post('/api/campaign/run')
def run_campaign(request:Request,source:str='demo',enrich_skills:bool=True,analyze_market:bool=True):
 return _consume(_campaign_steps(source,enrich_skills,analyze_market,_uid(current_user(request,db()))))
@app.post('/api/campaign/run/stream')
def run_campaign_stream(request:Request,source:str='demo',enrich_skills:bool=True,analyze_market:bool=True):
 uid=_uid(current_user(request,db()))
 def gen():yield from _campaign_steps(source,enrich_skills,analyze_market,uid)
 return _stream(_safe(gen()))
@app.post('/api/import')
def import_manual(request:Request,file:UploadFile=File(...)):
 path=upload_path(file.filename,file.file.read());d=db();u=current_user(request,d);uid=_uid(u);prof=_profile_query(d,uid).filter_by(status='validated').first();n=upsert_jobs(d,read_records(path),profile_data=prof.data if prof else None);d.add(Event(user_id=uid,kind='import',message=f'{n} offre(s) importée(s)'));d.commit();return {'imported':n}
def _import_url_steps(payload:OfferUrlImport, uid=None):
 """Ajout d'une annonce repérée par l'utilisateur, à partir de son lien.

 Pipeline identique à la veille sur une seule annonce : récupération fair use de
 la page (robots.txt, quota et cache partagés), extraction factuelle, pré-notation
 heuristique, complétion des champs manquants (heuristique + LLM light si
 configuré), note finale pondérée, puis ajout à la liste.
 """
 d=db();prof=_profile_query(d,uid).filter_by(status='validated').first()
 if not prof:
  yield P.error('Profil non validé : la notation de l’annonce a besoin du profil (métiers cibles, compétences, zone).');return
 link=extract_url(payload.url)
 if not link:
  yield P.error('Collez le lien complet de l’annonce (https://…) ou réutilisez l’import de fichier.');return
 site=source_label(link) or link
 overrides={'title':payload.title,'company':payload.company,'description':payload.description}
 yield P.progress('fetch',f'Récupération de la page {site} (une seule requête : robots.txt, quota et cache partagés avec la veille)…',source=site,url=link)
 try:
  offer,meta=fetch_offer(link,overrides=overrides)
 except (ValueError,RuntimeError) as exc:  # lien invalide, réseau, robots.txt, anti-bot…
  yield P.error(str(exc));return
 found=', '.join(meta.get('extraction') or []) or 'aucune donnée d’annonce détectée'
 yield P.progress('extract',f'Page analysée ({found}). Intitulé retenu : {offer.get("title") or "introuvable"}.',extraction=meta.get('extraction') or [])
 if not str(offer.get('title') or '').strip():
  yield P.error('Intitulé du poste introuvable dans cette page : collez le lien d’une annonce précise, ou renseignez l’intitulé en repli.');return
 yield P.progress('prescore','Pré-notation heuristique puis complétion des champs manquants (heuristique + LLM light si configuré)…')
 try:
  result=store_offer(d,offer,meta,prof.data,uid)
 except ValueError as exc:
  yield P.error(str(exc));return
 report=result['report'];job=result['job']
 verb='ajoutée' if report['created'] else 'mise à jour'
 note=f'pré-note {report["prescore"]}/10 → note {report["score"]}/10' if report['prescore'] is not None else f'note {report["score"]}/10'
 yield P.progress('imported',f'Annonce {verb} : {job["title"]} ({job["company"] or "entreprise inconnue"}) — {note}.',created=report['created'])
 for warning in report['warnings']:
  yield P.progress('warning',warning)
 yield P.done(result)
@app.post('/api/jobs/import-url')
def import_job_url(payload:OfferUrlImport, request:Request):
 return _consume(_import_url_steps(payload,_uid(current_user(request,db()))))
@app.post('/api/jobs/import-url/stream')
def import_job_url_stream(payload:OfferUrlImport, request:Request):
 uid=_uid(current_user(request,db()))
 def gen():yield from _import_url_steps(payload,uid)
 return _stream(_safe(gen()))
@app.get('/api/jobs')
def jobs(request:Request,min_score:float=0,decision:str='',application:str=''):
 d=db();u=current_user(request,d);uid=_uid(u)
 ratings=ratings_map(d,uid)
 all_jobs=d.query(Job).order_by(Job.id).all()
 idx={}
 qapp=d.query(Application)
 if uid is None and auth_enabled():qapp=qapp.filter(Application.user_id.is_(None))
 elif uid is not None:qapp=qapp.filter_by(user_id=uid)
 for a in qapp.all():
  idx.setdefault(a.job_id, []).append({'id': a.id, 'state': a.state, 'status': a.status})
 out=[]
 for j in all_jobs:
  r=ratings.get(j.id)
  score,confidence,dec,expl=overlay_job(j,r)
  if (score or 0)<min_score:continue
  if decision and dec!=decision:continue
  apps=idx.get(j.id,[])
  if application=='yes' and not apps: continue
  if application=='no' and apps: continue
  out.append({'id':j.id,'title':j.title,'company':j.company,'location':j.location,'remote':j.remote,'contract':j.contract,'score':score,'confidence':confidence,'decision':dec,'skills':j.skills,'sectors':j.sectors,'salary_min':j.salary_min,'salary_max':j.salary_max,'url':j.url,'description':j.description,'explanation':expl,'created_at':j.created_at.isoformat() if j.created_at else None,'applications':apps,'has_application':bool(apps),'application_id':apps[0]['id'] if apps else None})
 out.sort(key=lambda x:((x['score'] or 0),x['id']),reverse=True)
 return out
@app.post('/api/jobs/{job_id}/decision')
def decision(job_id:int,value:str,request:Request):
 d=db();u=current_user(request,d);uid=_uid(u)
 if value not in ('retained','ignored','new'):raise HTTPException(400,'Décision invalide')
 j, r = get_or_create_rating(d, uid, job_id)
 if not j:raise HTTPException(404,'Offre inconnue')
 if r is not None:r.decision=value
 else:j.decision=value
 d.add(Event(user_id=uid,kind='decision',message=f'{j.title}: {value}',metadata_json={'job_id':job_id}));d.commit();return {'ok':True}
@app.get('/api/debug/cache')
def debug_cache_list(request:Request,limit:int=200,source:str=''):
 d=db();current_user(request,d)
 from .cache_debug import list_entries
 return {'entries':list_entries(limit=limit or 200,source=source or None)}
@app.post('/api/debug/cache/replay',summary='Rejouer le cache connecteurs')
def debug_cache_replay(payload:CacheReplayRequest,request:Request):
 d=db();u=current_user(request,d);uid=_uid(u)
 from .cache_debug import replay_all
 res=replay_all(limit=int(payload.limit or 200),source=payload.source or None,db=d)
 if payload.import_missing:
  from .sources import upsert_jobs
  p=_profile_query(d,uid).filter_by(status='validated').first()
  res['imported']=upsert_jobs(d,res.get('missing') or [],profile_data=(p.data if p else None))
 return res
@app.get('/api/market')
def market(request:Request,summary:bool=False,analysis:bool=False):
 data=analyze(db())
 if summary: data['summary']=summarize(data)
 if analysis:
  d=db();u=current_user(request,d);p=_profile_query(d,_uid(u)).filter_by(status='validated').first()
  data['analysis']=market_analyzer(p.data if p else {},d)
 return data
@app.get('/',response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={}
    )
# Candidatures assistées (faits uniquement, auditables, connecteur mock)
from .applications import (
    create_application, generate, prepare, transition, create_email_draft,
    facts_for, build_instruction, quality, save_generated, save_content, app_payload, job_payload,
)
from .llm import get_provider
from .models import Application, Audit, EmailDraft
from .schemas import StateUpdate, ApplicationContentUpdate
from .tracking import status_history, history_index
def _job_map(d):
 return {j.id: j for j in d.query(Job).all()}

def _application_payload(d, a, jobs=None, histories=None, uid=None):
 """Dossier complet + chronologie datée (création, statuts, relances, envois)."""
 j=(jobs or {}).get(a.job_id) if jobs is not None else d.get(Job, a.job_id)
 hist=(histories or {}).get(a.id) if histories is not None else status_history(d, a.id, uid)
 return app_payload(a, j, history=hist)

def _user_applications(d, uid):
 q=d.query(Application)
 if uid is None and auth_enabled():return q.filter(Application.user_id.is_(None))
 if uid is None:return q
 return q.filter_by(user_id=uid)
def _get_owned_application(d, uid, application_id):
 a=d.get(Application, application_id)
 if not a:return None
 if uid is None:return a if (not auth_enabled() or a.user_id is None) else None
 return a if a.user_id==uid else None
def _require_application(d, uid, application_id):
 a=_get_owned_application(d, uid, application_id)
 if not a:raise HTTPException(404,'Dossier introuvable')
 return a
def _application_index(d, uid=None):
 idx = {}
 q=_user_applications(d,uid) if uid is not None or auth_enabled() else d.query(Application)
 for a in q.all():
  idx.setdefault(a.job_id, []).append({'id': a.id, 'state': a.state, 'status': a.status})
 return idx

@app.get('/api/applications')
def list_applications(request:Request):
 d=db();u=current_user(request,d);uid=_uid(u);jobs=_job_map(d);hist=history_index(d,None,uid); return [_application_payload(d,a,jobs,hist) for a in _user_applications(d,uid).order_by(Application.id.desc()).all()]

@app.get('/api/applications/{application_id}')
def get_application(application_id:int, request:Request):
 d=db();u=current_user(request,d);uid=_uid(u);a=_require_application(d,uid,application_id)
 return _application_payload(d,a,None,None,uid)

@app.put('/api/applications/{application_id}/content')
def update_application_content(application_id:int, payload:ApplicationContentUpdate, request:Request):
 d=db();u=current_user(request,d);uid=_uid(u)
 try:
  _require_application(d,uid,application_id)
  a=save_content(d, application_id, pitch=payload.pitch, email_subject=payload.email_subject, email_body=payload.email_body, cv_after=payload.cv_after, skills_to_highlight=payload.skills_to_highlight)
  return _application_payload(d,a,None,None,uid)
 except ValueError as e: raise HTTPException(400,str(e))

@app.post('/api/applications')
def new_application(job_id:int, request:Request):
 d=db();u=current_user(request,d)
 try: a=create_application(d,job_id,_uid(u)); return {'id':a.id,'state':a.state}
 except ValueError as e: raise HTTPException(400,str(e))
@app.post('/api/applications/{application_id}/generate')
def generate_application(application_id:int, request:Request):
 d=db();u=current_user(request,d);uid=_uid(u)
 try:
  _require_application(d,uid,application_id)
  a=generate(d,application_id); return _application_payload(d,a,None,None,uid)
 except (ValueError,AttributeError) as e: raise HTTPException(400,str(e))
def _generate_events(d, application_id, uid=None):
 app=_get_owned_application(d,uid,application_id) if uid is not None or auth_enabled() else d.get(Application,application_id)
 if not app:
  yield P.error('Dossier introuvable');return
 job=d.get(Job,app.job_id);profile=d.get(Profile,app.profile_id)
 if not job or not profile:
  yield P.error('Offre ou profil introuvable');return
 facts=facts_for(job,profile)
 if not facts['job_title']:
  yield P.error("Offre sans intitulé : impossible de rédiger la candidature.");return
 instruction=build_instruction(job,facts)
 yield P.progress('facts',f'Adéquation profil / offre établie ({len(app.skills_to_highlight or facts["matched_skills"])} compétence(s) commune(s)).')
 yield P.progress('pitch','Rédaction du pitch et de l’e-mail (modèle léger)…')
 try:
  result=get_provider().generate(instruction,facts)
 except Exception as exc:
  yield P.error(str(exc));return
 yield P.progress('cv','Génération du CV optimisé ATS (modèle puissant)…')
 try:
  cv=get_provider().generate_cv(facts)
 except Exception as exc:
  yield P.error(str(exc));return
 yield P.progress('verify','Vérification de la factualité et enregistrement…')
 q=quality(job,profile,result.get('pitch','')+' '+result.get('email_body','')+' '+cv.get('cv_ats',''))
 try:
  a=save_generated(d,application_id,result,cv,q)
 except ValueError as exc:
  yield P.error(str(exc));return
 yield P.done(_application_payload(d,a,None,None,uid))
@app.post('/api/applications/{application_id}/generate/stream')
def generate_application_stream(application_id:int, request:Request):
 uid=_uid(current_user(request,db()))
 def gen():
  d=db()
  yield from _generate_events(d,application_id,uid)
 return _stream(_safe(gen()))
def _candidater_steps(job_id, uid=None):
 d=db()
 yield P.progress('create','Création du dossier de candidature…')
 try:
  app=create_application(d,job_id,uid)
 except ValueError as exc:
  yield P.error(str(exc));return
 yield P.progress('created',f'Dossier #{app.id} créé, lancement de la génération…')
 yield from _generate_events(d,app.id,uid)
@app.post('/api/applications/candidater/stream')
def candidater_stream(job_id:int, request:Request):
 uid=_uid(current_user(request,db()))
 def gen():yield from _candidater_steps(job_id,uid)
 return _stream(_safe(gen()))
@app.post('/api/applications/{application_id}/state')
def update_state(application_id:int,payload:StateUpdate, request:Request):
 d=db();uid=_uid(current_user(request,d))
 try:
  _require_application(d,uid,application_id)
  a=transition(d,application_id,payload.state); return {'id':a.id,'state':a.state}
 except ValueError as e: raise HTTPException(400,str(e))
@app.post('/api/applications/{application_id}/email-draft')
def email_draft(application_id:int, request:Request):
 d=db();uid=_uid(current_user(request,d))
 try:
  _require_application(d,uid,application_id)
  e=create_email_draft(d,application_id); return {'id':e.id,'application_id':e.application_id,'provider':e.provider,'sent':e.sent,'subject':e.subject,'body':e.body}
 except ValueError as ex: raise HTTPException(400,str(ex))
@app.get('/api/audit')
def audit_log(request:Request):
 d=db();u=current_user(request,d);uid=_uid(u)
 q=d.query(Audit)
 if uid is None and auth_enabled():q=q.filter(Audit.user_id.is_(None))
 elif uid is not None:q=q.filter_by(user_id=uid)
 return [{'id':x.id,'kind':x.kind,'message':x.message,'metadata':x.metadata_json,'created_at':x.created_at.isoformat()} for x in q.order_by(Audit.id.desc()).all()]

# Boucle Rédacteur ↔ Évaluateur (LangGraph) : pitch / CV ATS itératifs
from .refinement import run_refinement, revise_with_user_comments, refine_payload
from .schemas import RefineFeedback
@app.post('/api/applications/{application_id}/refine')
def refine_application(application_id:int, request:Request, document_type:str='pitch'):
 d=db();uid=_uid(current_user(request,d))
 try:
  _require_application(d,uid,application_id)
  s=run_refinement(d,application_id,document_type)
  payload=refine_payload(s)
  a=d.get(Application, application_id)
  if a: payload['application']=_application_payload(d,a,None,None,uid)
  return payload
 except ValueError as e: raise HTTPException(400,str(e))
 except RuntimeError as e: raise HTTPException(502,str(e))
@app.post('/api/applications/{application_id}/refine/feedback')
def refine_application_feedback(application_id:int, payload:RefineFeedback, request:Request):
 d=db();uid=_uid(current_user(request,d))
 try:
  _require_application(d,uid,application_id)
  s=revise_with_user_comments(d,application_id,payload.comments,payload.document_type)
  out=refine_payload(s)
  a=d.get(Application, application_id)
  if a: out['application']=_application_payload(d,a,None,None,uid)
  return out
 except ValueError as e: raise HTTPException(400,str(e))
 except RuntimeError as e: raise HTTPException(502,str(e))

# Suivi des candidatures : connecteurs OAuth, envoi sous approbation explicite, statuts/Kanban, relances et rapports
from fastapi import Header
from .tracking import approve_and_send, create_followup, approve_followup, update_status, report
# Connexion OAuth Claude (Anthropic) : Authorization Code + PKCE -> bearer, sans x-api-key
from .providers import claude_oauth
from fastapi.responses import HTMLResponse
@app.get('/api/oauth/claude/authorize')
def claude_authorize():
 try: return claude_oauth.start_login()
 except RuntimeError as e: raise HTTPException(503,str(e))
@app.get('/api/oauth/claude/status')
def claude_status(): return claude_oauth.status()
@app.post('/api/oauth/claude/logout')
def claude_logout(): return claude_oauth.logout()
@app.get('/api/oauth/callback')
@app.get('/callback')
def claude_oauth_callback(code:str='',state:str='',error:str=''):
 if error: return HTMLResponse(f'<h3>Connexion Claude annulée</h3><p>{error}</p>',status_code=400)
 if not code: return HTMLResponse('<h3>Code OAuth absent</h3>',status_code=400)
 try: st=claude_oauth.exchange_code(code,state)
 except Exception as ex: return HTMLResponse(f'<h3>Échec de l’échange OAuth</h3><p>{ex}</p>',status_code=502)
 email=st.get('email') or 'Compte Claude'
 return HTMLResponse(f'<h3>✓ {email} connecté</h3><p>Vous pouvez fermer cet onglet. '
  f'Activez <code>LLM_PROVIDER=anthropic</code> dans le .env pour router les appels LLM via ce compte.</p>')
@app.get('/api/oauth/{provider}/authorize')
def oauth_authorize(provider:str):
 from .providers.email import provider_for
 p=provider_for(provider)
 if not p: raise HTTPException(400,'Provider inconnu')
 try: return {'provider':provider,'authorization_url':p.authorization_url('jobwatch-local-state')}
 except RuntimeError as e: raise HTTPException(503,str(e))
@app.post('/api/applications/{application_id}/send')
def send_application(application_id:int, request:Request, provider:str='mock', to:str='', idempotency_key:str=Header(default='',alias='Idempotency-Key')):
 d=db();uid=_uid(current_user(request,d))
 try:
  _require_application(d,uid,application_id)
  e=approve_and_send(d,application_id,provider,to,idempotency_key,uid); return {'id':e.id,'sent':e.sent,'provider':e.provider,'provider_message_id':e.provider_message_id,'attachment_path':e.attachment_path}
 except (ValueError,RuntimeError) as e: raise HTTPException(400,str(e))
@app.post('/api/applications/{application_id}/followup')
def schedule_followup(application_id:int, request:Request):
 d=db();uid=_uid(current_user(request,d))
 try:
  _require_application(d,uid,application_id)
  a=create_followup(d,application_id,None,uid); return {'id':a.id,'due_at':a.followup_due_at.isoformat(),'state':a.followup_state}
 except ValueError as e: raise HTTPException(400,str(e))
@app.post('/api/applications/{application_id}/followup/approve')
def approve_scheduled_followup(application_id:int, request:Request):
 d=db();uid=_uid(current_user(request,d))
 try:
  _require_application(d,uid,application_id)
  a=approve_followup(d,application_id,uid); return {'id':a.id,'state':a.followup_state}
 except ValueError as e: raise HTTPException(400,str(e))
@app.post('/api/applications/{application_id}/status')
def status_application(application_id:int, status:str, request:Request):
 d=db();uid=_uid(current_user(request,d))
 try:
  _require_application(d,uid,application_id)
  a=update_status(d,application_id,status,uid); return {'id':a.id,'status':a.status}
 except ValueError as e: raise HTTPException(400,str(e))
KANBAN_COLUMNS=['draft','submitted','screening','interview','offer','rejected','withdrawn','closed']
@app.get('/api/kanban')
def kanban(request:Request):
 d=db();u=current_user(request,d);uid=_uid(u);jobs={j.id:j for j in d.query(Job).all()};hist=history_index(d,None,uid);cards=[]
 ratings=ratings_map(d,uid)
 for a in _user_applications(d,uid).order_by(Application.kanban_order,Application.id).all():
  j=jobs.get(a.job_id);history=hist.get(a.id,[])
  # Dates utiles au board : création du dossier et dernier mouvement de statut.
  created=next((h['at'] for h in history if h['kind']=='application_created'),None)
  moved=next((h['at'] for h in reversed(history) if h['kind'] in ('status_changed','state_change','email_sent')),None)
  r=ratings.get(a.job_id) if j else None
  score=(r.score if r is not None and r.score is not None else (j.score if j else None))
  cards.append({'id':a.id,'job_id':a.job_id,'state':a.state,'status':a.status,'followup_state':a.followup_state,'followup_due_at':a.followup_due_at.isoformat() if a.followup_due_at else None,'submitted_at':a.submitted_at.isoformat() if a.submitted_at else None,'job_title':j.title if j else '','company':j.company if j else '','location':j.location if j else '','contract':j.contract if j else '','score':score,'email_subject':a.email_subject or '','pitch':a.pitch or '','job_created_at':j.created_at.isoformat() if j and j.created_at else None,'application_created_at':created,'status_updated_at':moved})
 return {'columns':KANBAN_COLUMNS,'cards':cards}
@app.get('/api/report')
def application_report(request:Request):
 d=db();u=current_user(request,d);return report(d,_uid(u))

@app.get('/api/overview')
def overview(request:Request):
 d=db();u=current_user(request,d);uid=_uid(u);p=_profile_query(d,uid).first();jobs=d.query(Job).all();ratings=ratings_map(d,uid);apps=_user_applications(d,uid).all()
 def _score(j):
  r=ratings.get(j.id)
  return r.score if r is not None and r.score is not None else j.score
 def _dec(j):
  r=ratings.get(j.id)
  return r.decision if r is not None and r.decision else j.decision
 return {'profile_status':p.status if p else 'draft','jobs_count':len(jobs),'retained_count':sum(_dec(j)=='retained' for j in jobs),'high_score_count':sum((_score(j) or 0)>=7 for j in jobs),'applications_count':len(apps),'to_validate_count':sum(a.state=='to_validate' for a in apps),'market':analyze(d),'report':report(d,uid)}
