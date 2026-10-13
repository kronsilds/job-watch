# Install Job Watch (Windows PowerShell)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host "==> Création de l'environnement virtuel (.venv)"
py -3.14 -m venv .venv

Write-Host "==> Activation de l'environnement"
& ".\.venv\Scripts\Activate.ps1"

Write-Host "==> Mise à jour de pip"
python -m pip install --upgrade pip

Write-Host "==> Installation des dépendances"
pip install -r requirements.txt

Write-Host ""
Write-Host "Installation terminée. Démarrage :"
Write-Host "  .\start.ps1"
Write-Host "  puis http://127.0.0.1:8000  (docs : /docs)"