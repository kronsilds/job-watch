"""Sonde UI Playwright du profil (outil manuel, hors suite pytest).

Vérifie en conditions réelles que l'import CV et l'affinage IA alimentent toute
la fiche : résumé généré, compétences sans doublon face à la matrice, champs
secteurs/exclusions/salaires/métiers visés affichés, matrice regroupée et
consigne IA branchée sur `POST /api/profile/refine`.

Pré-requis : `pip install playwright` (réutilise Edge si Chromium n'est pas
téléchargé). Lance d'abord le serveur sur une base jetable, puis :

    JOBWATCH_DATABASE_URL=sqlite:///.../tests/_shots/e2e.db \
      ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8011
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe tests/_profile_probe.py

Les captures et la base de test sont écrites dans tests/_shots/ (non versionné).
"""
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8011"
OUT = Path(__file__).resolve().parent / "_shots"

# Fiche type renvoyée par un LLM : liste plate bruitée (minuscules, doublons de
# mots-clés) + matrice détaillée, et aucun résumé (cas de l'import défaillant).
LLM_FICHE = {
    "name": "Olivier ROUGERON",
    "main_job": "Architecte Logiciel / Tech Lead – IA appliquée au développement",
    "secondary_job": "Directeur informatique",
    "jobs": ["Architecte Logiciel / Tech Lead – IA appliquée au développement",
             "Architecte logiciel", "Tech Lead", "Directeur informatique"],
    "adjacent_jobs": ["Scrum Master", "Expert IA appliquée au développement"],
    "metiers_vises": [], "sources": [], "seniorite": ["senior", "lead"],
    "locations": ["Paris", "Île-de-France"], "mobility": "toute la France si remote",
    "remote": ["remote", "hybride"], "contracts": ["Indépendant", "CDI", "CDD"],
    "sectors": ["Industrie du packaging", "Télécommunications / VOD", "CRM"],
    "exclusions": ["défense"], "years_experience": 25.0, "profile_summary": "",
    "salary_min": 70000, "salary_max": 95000,
    "skills": ["sql", "r", "react", "css", "git", "azure", "rest", "crm",
               "Architecture logicielle", "Serveurs MCP", "TypeScript"],
    "skill_matrix": [
        {"skill": "Architecture logicielle", "category": "Architecture logicielle",
         "level": "expert", "evidence": "25 ans d'architecture logicielle."},
        {"skill": "Serveurs MCP", "category": "Intelligence artificielle",
         "level": "confirmé", "evidence": "Intégration de serveurs MCP."},
        {"skill": "TypeScript", "category": "Langages", "level": "confirmé",
         "evidence": "Langages : TypeScript."},
        {"skill": "SQL Server", "category": "Bases de données", "level": "confirmé",
         "evidence": "Environnement technique SQL Server."},
        {"skill": "React 19", "category": "Frameworks", "level": "confirmé",
         "evidence": "Environnement technique React 19."},
        {"skill": "Git / Git Flow", "category": "DevOps", "level": "confirmé",
         "evidence": "Outils & cloud : Git / Git Flow."},
        {"skill": "Microsoft Azure", "category": "Cloud", "level": "confirmé",
         "evidence": "Environnement technique Azure."},
        {"skill": "API REST", "category": "Architecture / Développement",
         "level": "confirmé", "evidence": "API REST en .NET."},
    ],
}

CV_TEXT = """Olivier ROUGERON
Architecte Logiciel / Tech Lead
25 ans d'expérience en architecture logicielle et développement.
Compétences : SQL Server, React 19, TypeScript, Git / Git Flow, Microsoft Azure,
Docker, Node.js, PostgreSQL, Python (R occasionnel), API REST.
Paris, Île-de-France. Remote ou hybride acceptés. Contrats : CDI, Indépendant.
"""


def check(label, ok, detail=""):
    print(f"{'OK ' if ok else 'KO '} {label}{(' — ' + detail) if detail else ''}")
    return bool(ok)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    failures = []

    # 1) Fiche « LLM » déposée via l'API : normalisation serveur attendue.
    r = httpx.put(f"{BASE}/api/profile", timeout=30, json={
        "name": LLM_FICHE["name"], "data": LLM_FICHE, "validate_profile": True,
        "source_text": CV_TEXT})
    data = r.json().get("data", {})
    skills = [s.lower() for s in data.get("skills", [])]
    failures.append(not check("PUT /api/profile : résumé généré",
                             bool(data.get("profile_summary")),
                             data.get("profile_summary", "")[:90]))
    failures.append(not check("PUT /api/profile : doublons absorbés par la matrice",
                             not {"sql", "react", "git", "azure", "rest"} & set(skills),
                             str(data.get("skills"))))
    failures.append(not check("PUT /api/profile : langage R conservé", "r" in skills))

    cv_file = OUT / "probe_cv.txt"
    cv_file.write_text(CV_TEXT, encoding="utf-8")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except Exception:
            browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(800)
        page.click('[data-tab="profile"]')  # atterrissage contextuel : on force l'onglet Profil
        page.wait_for_timeout(400)

        # 2) La fiche affiche l'analyse complète, résumé compris.
        failures.append(not check("résumé visible dans la fiche",
                                 bool(page.input_value("#profile_summary").strip())))
        rendered = [s.lower() for s in page.input_value("#skills").split("\n") if s.strip()]
        failures.append(not check("compétences affichées sans doublon",
                                 not {"sql", "react", "git", "azure", "rest"} & set(rendered),
                                 str(rendered)))
        for field in ("metiers_vises", "sectors", "exclusions", "salary_min", "salary_max"):
            failures.append(not check(f"champ {field} présent",
                                     page.locator(f"#{field}").count() == 1))
        matrix_rows = page.locator("#skillMatrix .matrix-row").count()
        levels = page.locator("#skillMatrix select.matrix-level").count()
        failures.append(not check("matrice regroupée et complète",
                                 matrix_rows == len(LLM_FICHE["skill_matrix"]) == levels,
                                 f"{matrix_rows} ligne(s) / {levels} sélecteur(s)"))
        failures.append(not check("matrice groupée par domaine",
                                 page.locator("#skillMatrix .matrix-group").count() >= 2))

        # 2b) Le ciblage avancé est éditable et prérempli (secteurs, exclusions, salaires).
        page.click("#profileForm details.profile-details >> nth=0")
        page.wait_for_timeout(300)
        failures.append(not check("secteurs préremplis",
                                 "Télécommunications / VOD" in page.input_value("#sectors")))
        failures.append(not check("exclusions préremplies",
                                 "défense" in page.input_value("#exclusions")))
        failures.append(not check("salaires préremplis",
                                 page.input_value("#salary_min") == "70000"
                                 and page.input_value("#salary_max") == "95000",
                                 f"{page.input_value('#salary_min')} / {page.input_value('#salary_max')}"))
        page.locator("#profileForm").screenshot(path=str(OUT / "53-profile-form.png"))

        # 3) Le prompt IA est branché : la fiche est réécrite par le modèle (ou
        #    signalée comme inchangée sans modèle), jamais laissée en attente.
        page.fill("#profileAiPrompt", "Recentre la cible sur des postes de Tech Lead à Paris")
        page.click("#profileAiBox button.primary")
        try:
            page.wait_for_function(
                "() => {const t=document.getElementById('profileAiStatus').textContent;"
                " return !!t && !t.includes('applique votre consigne')}", timeout=180000)
            status, ok = page.inner_text("#profileAiStatus"), True
        except Exception as exc:  # délai dépassé : l'appel n'a pas abouti
            status, ok = f"délai dépassé ({exc})", False
        failures.append(not check("prompt IA câblé (réponse affichée)", ok, status[:160]))
        failures.append(not check("prompt IA : résumé toujours présent",
                                 bool(page.input_value("#profile_summary").strip())))
        page.screenshot(path=str(OUT / "50-profile-refined.png"), full_page=True)

        # 4) Import d'un CV : résumé + matrice re-rendus depuis l'analyse.
        page.set_input_files("#cvFile", str(cv_file))
        try:
            page.wait_for_function(
                "() => document.getElementById('cvResult').textContent.includes('Analyse prête')",
                timeout=180000)
            finished = True
        except Exception as exc:
            finished = False
        failures.append(not check("import CV : analyse terminée", finished))
        failures.append(not check("import CV : résumé régénéré",
                                 bool(page.input_value("#profile_summary").strip()),
                                 page.input_value("#profile_summary")[:90]))
        page.click("#matrixSummary")
        page.wait_for_timeout(400)
        rows = page.locator("#skillMatrix .matrix-row").count()
        failures.append(not check("import CV : matrice affichée", rows > 3, f"{rows} ligne(s)"))
        failures.append(not check("import CV : niveaux modifiables",
                                 page.locator("#skillMatrix select.matrix-level").count() == rows))
        page.screenshot(path=str(OUT / "51-profile-cv-import.png"), full_page=True)
        page.locator(".cv-card").screenshot(path=str(OUT / "52-cv-card-matrix.png"))
        browser.close()

    print("échecs :", sum(1 for f in failures if f))
    return 1 if any(failures) else 0


if __name__ == "__main__":
    sys.exit(main())