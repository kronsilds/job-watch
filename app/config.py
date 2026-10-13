import os
from pathlib import Path
BASE_DIR=Path(__file__).resolve().parents[1]
DATABASE_URL=os.getenv("JOBWATCH_DATABASE_URL",f"sqlite:///{BASE_DIR}/jobwatch.db")
DEMO_PATH=BASE_DIR/os.getenv("JOBWATCH_DEMO_PATH","demo/jobs.json")
