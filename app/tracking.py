"""Suivi des candidatures : correspondance sous approbation, relances, statuts, rapports et audit."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib, json
from .models import Application, EmailDraft, Audit
from .providers.email import provider_for

def now(): return datetime.now(timezone.utc)

def audit(db, kind, message, meta=None, user_id=None):
 kw={}
 try:
  if user_id is not None and hasattr(Audit,'user_id'):kw['user_id']=user_id
 except Exception:kw={}
 db.add(Audit(kind=kind,message=message,metadata_json=meta or {},**kw))

# --- Chronologie datée d'un dossier -----------------------------------------
STATUS_LABELS={'draft':'Brouillon','submitted':'Envoyée','screening':'Présélection',
 'interview':'Entretien','offer':'Offre','rejected':'Refusée','withdrawn':'Retirée','closed':'Fermée'}
STATE_LABELS={'draft':'Brouillon','to_validate':'À valider','approved':'Approuvé',
 'rejected':'Rejeté','email_draft_created':'Brouillon e-mail','sent':'Envoyé'}
HISTORY_KINDS=('application_created','generation','content_edited','state_change',
 'status_changed','email_draft_created','email_sent','followup_scheduled','followup_approved')

def _history_label(kind, meta):
    if kind=='status_changed': return 'Statut : '+STATUS_LABELS.get(meta.get('status'), meta.get('status') or '—')
    if kind=='state_change': return 'État : '+STATE_LABELS.get(meta.get('state'), meta.get('state') or '—')
    return {'application_created':'Dossier créé','generation':'Contenus générés (pitch, e-mail, CV)',
     'content_edited':'Textes retravaillés manuellement','email_draft_created':'Brouillon e-mail créé',
     'email_sent':'Candidature envoyée','followup_scheduled':'Relance planifiée',
     'followup_approved':'Relance approuvée'}.get(kind, kind)

def _history_entry(row):
    meta=row.metadata_json or {}
    return {'kind':row.kind,'at':row.created_at.isoformat() if row.created_at else None,
     'label':_history_label(row.kind,meta),
     'status':meta.get('status') or '','state':meta.get('state') or ''}

def history_index(db, application_id=None, user_id=None):
    """Chronologie groupée par dossier (une seule requête), filtrée sur les
    entrées auditables qui portent un ``application_id``. Le tri suit l'ordre
    d'insertion de l'audit (donc l'ordre réel des événements).

    L'isolation se fait **par dossier** (l'appelant ne demande que ses propres
    dossiers) : on ne filtre pas sur ``Audit.user_id`` car une entrée historique
    peut porter ``user_id IS NULL`` tout en appartenant à un dossier existant.
    """
    out={}
    rows=db.query(Audit).filter(Audit.kind.in_(HISTORY_KINDS)).order_by(Audit.id).all()
    for row in rows:
        meta=row.metadata_json or {}
        try: aid=int(meta.get('application_id'))
        except (TypeError,ValueError): continue
        if application_id is not None and aid!=int(application_id): continue
        out.setdefault(aid,[]).append(_history_entry(row))
    return out

def status_history(db, application_id, user_id=None):
    """Chronologie datée d'un dossier : création, générations, statuts, relances."""
    return history_index(db, application_id, user_id).get(int(application_id), [])

def attach_cv(db, app):
    root=Path(__file__).resolve().parents[1]/'data'/'attachments'; root.mkdir(parents=True,exist_ok=True)
    path=root/f'application-{app.id}-cv.txt'; path.write_text(app.cv_after or app.cv_before or 'CV non disponible',encoding='utf-8'); return str(path)

def _app_user(a):
 try:return getattr(a,'user_id',None)
 except Exception:return None
def create_followup(db, application_id, due_at=None, user_id=None):
    a=db.get(Application,application_id)
    if not a: raise ValueError('Application inconnue')
    a.followup_due_at=due_at or now()+timedelta(days=7); a.followup_state='pending_approval'; audit(db,'followup_scheduled',f'Relance à approuver pour {application_id}',{'application_id':application_id,'due_at':a.followup_due_at.isoformat()},user_id if user_id is not None else _app_user(a)); db.commit(); return a

def approve_and_send(db, application_id, provider_name='mock', to='', idempotency_key='', user_id=None):
    if not idempotency_key:
        raise ValueError('Idempotency-Key requis')

    existing = db.query(EmailDraft).filter_by(
        application_id=application_id,
        idempotency_key=idempotency_key
    ).first()

    if existing and existing.sent:
        return existing

    a = db.get(Application, application_id)
    if not a or a.state != 'approved':
        raise ValueError('Approbation explicite requise')

    if not to:
        raise ValueError('Destinataire requis')

    draft = db.query(EmailDraft).filter_by(
        application_id=application_id,
        kind='application'
    ).first()

    if not draft:
        draft = EmailDraft(
            application_id=application_id,
            kind='application',
            to=to,
            subject=a.email_subject,
            body=a.email_body,
            provider=provider_name,
            sent=False,
            idempotency_key=idempotency_key,
            attachment_path=attach_cv(db, a)
        )
        db.add(draft)
        db.flush()

    if draft.sent:
        return draft

    p = provider_for(provider_name)
    if not p:
        raise ValueError('Provider inconnu')

    msg = p.send(
        to=draft.to,
        subject=draft.subject,
        body=draft.body,
        attachment_path=draft.attachment_path,
        idempotency_key=idempotency_key
    )

    draft.sent = True
    draft.provider_message_id = msg.provider_message_id
    a.state = 'sent'
    a.status = 'submitted'
    a.submitted_at = now()

    audit(
        db,
        'email_sent',
        f'Candidature {application_id} envoyée',
        {
            'application_id': application_id,
            'provider': provider_name,
            'idempotency_key': idempotency_key
        },
        user_id if user_id is not None else _app_user(a)
    )

    db.commit()
    return draft

def approve_followup(db, application_id, user_id=None):
    a=db.get(Application,application_id)
    if not a or a.followup_state!='pending_approval': raise ValueError('Relance non planifiée')
    a.followup_state='approved'; audit(db,'followup_approved',f'Relance approuvée pour {application_id}',{'application_id':application_id},user_id if user_id is not None else _app_user(a)); db.commit(); return a

def due_followups(db, user_id=None):
 q=db.query(Application).filter(Application.followup_state=='approved',Application.followup_due_at<=now(),Application.state!='closed')
 if user_id is not None:
  try:q=q.filter_by(user_id=user_id)
  except Exception:pass
 return q.all()

def update_status(db, application_id, status, user_id=None):
    allowed={'draft','to_validate','approved','submitted','screening','interview','offer','rejected','withdrawn','closed'}
    if status not in allowed: raise ValueError('Statut invalide')
    a=db.get(Application,application_id)
    if not a: raise ValueError('Application inconnue')
    a.status=status; audit(db,'status_changed',f'Application {application_id}: {status}',{'application_id':application_id,'status':status},user_id if user_id is not None else _app_user(a)); db.commit(); return a

def report(db, user_id=None):
    rows=db.query(Application).all() if user_id is None else db.query(Application).filter_by(user_id=user_id).all(); counts={}
    for a in rows: counts[a.status]=counts.get(a.status,0)+1
    return {'generated_at':now().isoformat(),'total':len(rows),'by_status':counts,'due_followups':len(due_followups(db,user_id))}
