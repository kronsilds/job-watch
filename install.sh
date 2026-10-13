#!/bin/sh
# Install Job Watch (macOS / Linux)
set -e
cd "$(dirname "$0")"

echo "==> Création de l'environnement virtuel (.venv)"
python3 -m venv .venv
. .venv/bin/activate

echo "==> Mise à jour de pip"
python -m pip install --upgrade pip

echo "==> Installation des dépendances"
pip install -r requirements.txt

echo ""
echo "Installation terminée. Démarrage :"
echo "  ./start.sh"
echo "  puis http://127.0.0.1:8000  (docs : /docs)"