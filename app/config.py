import os
from pathlib import Path
from dotenv import load_dotenv
BASE_DIR=Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR/".env")
def _normalize_db_url(url:str)->str:
 """Neon/Vercel fournissent postgres:// ou postgresql:// ; SQLAlchemy doit viser le driver psycopg (v3)."""
 for prefix in ("postgres://","postgresql://"):
  if url.startswith(prefix):return "postgresql+psycopg://"+url[len(prefix):]
 return url
DATABASE_URL=_normalize_db_url(os.getenv("JOBWATCH_DATABASE_URL",f"sqlite:///{BASE_DIR}/jobwatch.db"))
DEMO_PATH=BASE_DIR/os.getenv("JOBWATCH_DEMO_PATH","demo/jobs.json")
