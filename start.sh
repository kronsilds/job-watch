#!/bin/sh
# Start Job Watch (macOS / Linux)
set -e
cd "$(dirname "$0")"

if [ -d ".venv" ]; then
  . .venv/bin/activate
fi

python -m app.cli init
python -m app.cli search-now
HOST="${JOBWATCH_HOST:-0.0.0.0}"
PORT="${JOBWATCH_PORT:-8000}"
echo "Serveur sur http://$HOST:$PORT (LAN: http://192.168.1.83:$PORT)"
exec uvicorn app.main:app --host "$HOST" --port "$PORT"
