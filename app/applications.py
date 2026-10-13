"""Fact-based application assistance and explicit state machine."""
import hashlib, json, re
from datetime import datetime, timezone
from .models import Job, Profile, Application, EmailDraft, Audit
from .skills import match_skills
from .llm import get_provider
STATES={'draft','to_validate','approved','rejected','email_draft_created'}
TRANSITIONS={('draft','to_validate'),('to_validate','approved'),('to_validate','rejected'),('approved','email_draft_created')}
def audit(db, kind, message, meta=None, user_id=None):
 kw={}
 try:
  if user_id is not None and hasattr(Audit,'user_id'):kw['user_id']=user_id
 except Exception:kw={}
 db.add(Audit(kind=kind,message=message,metadata_json=meta or {},**kw))
def facts_for(job, profile):
    data = profile.data or {}
    # Appariement normalisé : les libellés du profil (« API REST », « .NET Core
    # (8-10) ») et ceux de l'offre (lexique nu : « rest », « dotnet ») ne
    # s'écrivent pas pareil (cf. app/skills.py).
    profile_skills = [str(x) for x in (data.get('skills') or []) if str(x).strip()]
    job_skills = [str(x) for x in (getattr(job, 'skills', None) or []) if str(x).strip()]
    matched, matched_job, _unmatched_profile, unmatched_job = match_skills(
        profile_skills, job_skills)
    return {
        'job_title': getattr(job, 'title', ''),
        'company': getattr(job, 'company', ''),
        'matched_skills': matched,
        'unmatched_job_skills': unmatched_job,
        'matched_job_skills': matched_job,
        'profile_skills': profile_skills,
        'job_skills': job_skills,
        'job_description': getattr(job, 'description', '') or '',
        'job_location': getattr(job, 'location', '') or '',
        'job_contract': getattr(job, 'contract', '') or '',
        'job_remote': getattr(job, 'remote', '') or '',
        'job_salary_min': getattr(job, 'salary_min', None),
        'job_salary_max': getattr(job, 'salary_max', None),
        'job_url': getattr(job, 'url', '') or '',
        'main_job': data.get('main_job') or '',
        'secondary_job': data.get('secondary_job') or '',
        'years_experience': data.get('years_experience'),
        'profile_summary': data.get('profile_summary') or '',
        'skill_matrix': data.get('skill_matrix') or [],
        'remote': data.get('remote') or [],
        'contracts': data.get('contracts') or [],
        'locations': data.get('locations') or [],
        'source_job_id': getattr(job, 'id', None),
        'source_profile_id': getattr(profile, 'id', None),
    }


def build_instruction(job, facts):
    instruction = f'Rédige une candidature pour le poste de {job.title or "ce poste"}'
    if job.company:
        instruction += f' chez {job.company}'
    if facts['job_description']:
        instruction += f'. Le poste consiste notamment à : {facts["job_description"]}'
    return instruction


def job_payload(job):
    if job is None:
        return None
    return {
        'id': job.id, 'external_id': job.external_id, 'title': job.title or '',
        'company': job.company or '', 'location': job.location or '',
        'remote': job.remote or '', 'contract': job.contract or '',
        'score': job.score, 'confidence': job.confidence, 'decision': job.decision,
        'skills': list(job.skills or []), 'sectors': list(job.sectors or []),
        'salary_min': job.salary_min, 'salary_max': job.salary_max,
        'url': job.url or '', 'description': job.description or '',
        'explanation': job.explanation or {},
        'created_at': job.created_at.isoformat() if getattr(job, 'created_at', None) else None,
    }


def app_payload(app, job=None, history=None):
    payload = {
        'id': app.id, 'job_id': app.job_id, 'state': app.state,
        'status': app.status, 'followup_state': app.followup_state,
        'followup_due_at': app.followup_due_at.isoformat() if app.followup_due_at else None,
        'submitted_at': app.submitted_at.isoformat() if app.submitted_at else None,
        'pitch': app.pitch, 'email_subject': app.email_subject,
        'email_body': app.email_body, 'skills_to_highlight': app.skills_to_highlight,
        'cv_before': app.cv_before, 'cv_after': app.cv_after,
        'diff': app.diff, 'quality': app.quality,
        'history': list(history or []),
    }
    if job is not None:
        payload['job'] = job_payload(job)
    return payload


def save_content(db, app_id, pitch=None, email_subject=None, email_body=None, cv_after=None, skills_to_highlight=None):
    app = db.get(Application, app_id)
    if not app:
        raise ValueError('Dossier introuvable')
    if pitch is not None:
        app.pitch = pitch
    if email_subject is not None:
        app.email_subject = email_subject
    if email_body is not None:
        app.email_body = email_body
    if cv_after is not None:
        app.cv_after = cv_after
    if skills_to_highlight is not None:
        app.skills_to_highlight = [str(s) for s in skills_to_highlight]
    audit(db, 'content_edited', f'Contenus re-travaillés manuellement pour application {app.id}', {'application_id': app.id}, getattr(app,'user_id',None))
    db.commit()
    return app


def save_generated(db, app_id, result, cv, q):
    """Persiste pitch + e-mail + CV ATS générés, puis bascule le dossier en
    `to_validate`. Factuel : tout vient du profil et de l'offre."""
    app = db.get(Application, app_id)
    profile = db.get(Profile, app.profile_id)
    job = db.get(Job, app.job_id)
    pdata = (profile.data if profile else {}) or {}
    app.pitch = result['pitch']
    app.email_subject = result['email_subject']
    app.email_body = result['email_body']
    app.cv_before = json.dumps({
        'skills': pdata.get('skills', []),
        'main_job': pdata.get('main_job', ''),
        'secondary_job': pdata.get('secondary_job', ''),
        'years_experience': pdata.get('years_experience'),
        'profile_summary': pdata.get('profile_summary', ''),
    }, ensure_ascii=False, sort_keys=True)
    cv_skills = [str(s) for s in cv.get('skills') or []]
    app.cv_after = cv.get('cv_ats') or app.cv_before
    app.skills_to_highlight = cv_skills or [str(s) for s in pdata.get('skills', [])][:10]
    app.diff = {
        'added': [str(h) for h in cv.get('highlights') or []] or cv_skills,
        'removed': [],
        'unchanged': ['Faits issus du profil, réordonnés pour le CV ATS.'],
    }
    facts = facts_for(job, profile) if job and profile else {}
    app.state = 'to_validate'
    app.fact_hash = hashlib.sha256(
        json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()
    audit(db, 'generation', f'Contenus générés pour application {app.id}',
          {'application_id': app.id, 'fact_hash': app.fact_hash,
           'provider': getattr(get_provider(), 'name', ''), 'warnings': q['warnings']}, getattr(app,'user_id',None))
    app.quality = q
    db.commit()
    return app


def prepare(db, app_id):
    """Orchestration pitch (léger) + CV ATS (heavy) + contrôle de factualité."""
    app = db.get(Application, app_id)
    if not app:
        raise ValueError('Dossier introuvable')
    job = db.get(Job, app.job_id)
    profile = db.get(Profile, app.profile_id)
    if not job or not profile:
        raise ValueError('Offre ou profil introuvable')
    facts = facts_for(job, profile)
    if not facts['job_title']:
        raise ValueError("Offre sans intitulé : impossible de rédiger la candidature.")
    instruction = build_instruction(job, facts)
    provider = get_provider()
    result = provider.generate(instruction, facts)
    cv = provider.generate_cv(facts)
    q = quality(job, profile, result['pitch'] + ' ' + result.get('email_body', ''))
    return save_generated(db, app_id, result, cv, q)


generate = prepare

def quality(job, profile, content):
 f=facts_for(job,profile); warnings=[]
 for skill in re.findall(r'\b[a-zA-Z][a-zA-Z0-9+#./ -]{1,30}\b', content):
  pass
 if not f['matched_skills'] and f['job_skills']:
  warnings.append('Aucune compétence du profil ne correspond aux compétences indexées de l’offre.')
 elif not f['job_skills']:
  # Rien à comparer : l'information n'est pas dans la source, ce n'est pas un
  # défaut de la candidature (et cela ne doit pas apparaître comme un échec).
  warnings.append('Compétences non indexées dans cette annonce : adéquation compétences non évaluable.')
 if not job.company: warnings.append('Entreprise non précisée dans la source de l’offre.')
 if not job.url: warnings.append('URL de l’offre absente.')
 # hard guard against common unsupported claims in generated text
 unsupported=[w for w in ('10 ans','manager','expert','diplômé') if w in content.lower() and w not in json.dumps(profile.data or {}).lower()]
 if unsupported: warnings.append('Terme potentiellement non prouvé : '+', '.join(unsupported))
 return {'ok':not warnings,'warnings':warnings,'facts':f,'checked_at':datetime.now(timezone.utc).isoformat()}
def create_application(db, job_id, user_id=None):
 job=db.get(Job,job_id)
 if user_id is None:
  profile=db.query(Profile).filter_by(status='validated').first()
 else:
  profile=db.query(Profile).filter_by(status='validated',user_id=user_id).first()
 if not job or not profile: raise ValueError('Offre inconnue ou profil non validé')
 kw={}
 try:
  if hasattr(Application,'user_id'):kw['user_id']=user_id
 except Exception:kw={}
 app=Application(job_id=job.id,profile_id=profile.id,state='draft',**kw);db.add(app);db.flush(); audit(db,'application_created',f'Application {app.id} créée',{'application_id':app.id,'job_id':job.id},user_id);db.commit();return app
def transition(db, app_id, target):
 app=db.get(Application,app_id)
 if not app or (app.state,target) not in TRANSITIONS: raise ValueError('Transition interdite')
 app.state=target; audit(db,'state_change',f'Application {app.id}: {target}',{'application_id':app.id,'state':target},getattr(app,'user_id',None));db.commit();return app
def create_email_draft(db, app_id):
 app=db.get(Application,app_id)
 if not app or app.state!='approved': raise ValueError('Application non approuvée')
 draft=EmailDraft(application_id=app.id,to='',subject=app.email_subject,body=app.email_body,provider='mock',sent=False);db.add(draft);app.state='email_draft_created';audit(db,'email_draft_created',f'Brouillon e-mail {draft.id} créé via mock (non envoyé)',{'application_id':app.id,'sent':False},getattr(app,'user_id',None));db.commit();return draft
