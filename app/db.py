from sqlalchemy import create_engine,event
from sqlalchemy.orm import DeclarativeBase,sessionmaker
from .config import DATABASE_URL
class Base(DeclarativeBase): pass
_IS_SQLITE=DATABASE_URL.startswith("sqlite")
engine=create_engine(DATABASE_URL,connect_args={"check_same_thread":False} if _IS_SQLITE else {})
if _IS_SQLITE:
 # Harden SQLite for concurrent access (multi-process / FastAPI threadpool):
 #  - WAL journal avoids the delete-mode lock churn behind "disk I/O error" on Windows.
 #  - busy_timeout makes concurrent writers wait & retry instead of failing immediately.
 #  - synchronous=NORMAL is safe with WAL and keeps writes fast.
 @event.listens_for(engine,"connect")
 def _set_sqlite_pragma(dbapi_connection,connection_record):
  cursor=dbapi_connection.cursor()
  cursor.execute("PRAGMA journal_mode=WAL")
  cursor.execute("PRAGMA busy_timeout=30000")
  cursor.execute("PRAGMA synchronous=NORMAL")
  cursor.close()
SessionLocal=sessionmaker(bind=engine,expire_on_commit=False)
def _ensure_user_columns():
 """Ajoute user_id aux tables personnelles + cree users/auth_tokens/job_ratings."""
 from sqlalchemy import inspect,text
 insp=inspect(engine)
 tables=set(insp.get_table_names())
 stmts=[]
 for table in ("profiles","events","applications","audits"):
  if table not in tables:continue
  cols={c["name"] for c in insp.get_columns(table)}
  if "user_id" not in cols:stmts.append(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
 need_tables=[t for t in ("users","auth_tokens","job_ratings") if t not in tables]
 if stmts or need_tables:
  from . import models as _models
  Base.metadata.create_all(engine,tables=[_models.User.__table__,_models.AuthToken.__table__,_models.JobRating.__table__])
  if stmts and _IS_SQLITE:
   with engine.begin() as conn:
    for s in stmts:conn.execute(text(s))
def _add_missing_columns():
 """Ajoute les colonnes apparues après coup : ``create_all`` ne modifie pas une
 table existante. SQLite uniquement (le schéma est recréé sur une base neuve)."""
 if not _IS_SQLITE:return
 from sqlalchemy import inspect,text
 insp=inspect(engine)
 if "jobs" not in insp.get_table_names():return
 cols={c["name"] for c in insp.get_columns("jobs")}
 if "created_at" in cols:return
 with engine.begin() as conn:
  conn.execute(text("ALTER TABLE jobs ADD COLUMN created_at DATETIME"))
  # Date d'ajout approximée par la 1re campagne importée, sinon l'instant de migration.
  conn.execute(text("UPDATE jobs SET created_at=(SELECT MIN(created_at) FROM events WHERE kind='campaign') WHERE created_at IS NULL"))
  conn.execute(text("UPDATE jobs SET created_at=COALESCE(created_at,CURRENT_TIMESTAMP) WHERE created_at IS NULL"))
def init_db():
 from . import models
 Base.metadata.create_all(engine)
 _ensure_user_columns()
 _add_missing_columns()
