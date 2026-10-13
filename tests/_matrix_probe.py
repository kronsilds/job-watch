"""Sonde UI Playwright de la matrice de compétences (outil manuel, hors pytest).

Vérifie que, dans chaque carte de compétence (page Profil), le sélecteur de niveau et
l'étiquette de catégorie sont rangés dans des colonnes fixes : quelles que soient la
longueur du libellé et celle de la preuve, tous les contrôles partagent la même
abscisse et la même largeur d'une ligne à l'autre. Sans ces pistes, un libellé long
renvoyait à la ligne suivante le niveau puis la catégorie (flex-wrap), si bien que
les cartes se décalaient les unes par rapport aux autres.

Pré-requis : `pip install playwright` (réutilise Edge si Chromium n'est pas
téléchargé). Lance d'abord le serveur sur une base jetable, puis :

    JOBWATCH_DATABASE_URL=sqlite:///.../tests/_shots/matrix.db \
      ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8012
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe tests/_matrix_probe.py

Les captures sont écrites dans tests/_shots/ (non versionné).
"""
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "_shots"
BASE = "http://127.0.0.1:8012"

# Libellés, catégories et preuves de longueurs très inégales : c'est ce mélange qui
# faisait « sauter » le niveau et la catégorie d'une carte à l'autre.
FICHE = {
    "name": "Olivier ROUGERON",
    "main_job": "Architecte Logiciel / Tech Lead",
    "secondary_job": "",
    "jobs": ["Architecte logiciel"],
    "skills": ["Go", "SQL Server", "React 19", "Microsoft Azure", "API REST",
               "Architecture logicielle appliquée aux systèmes distribués et aux agents IA",
               "Communication technique avec les directions métier et les équipes produit"],
    "skill_matrix": [
        {"skill": "Go", "category": "Langages", "level": "débutant",
         "evidence": "Notions."},
        {"skill": "SQL Server", "category": "Bases de données", "level": "confirmé",
         "evidence": "Tuning des requêtes et modélisation."},
        {"skill": "React 19", "category": "Frameworks", "level": "opérationnel",
         "evidence": "Interfaces de suivi de candidatures."},
        {"skill": "Architecture logicielle appliquée aux systèmes distribués et aux agents IA",
         "category": "Architecture / Développement", "level": "expert",
         "evidence": "25 ans d'architecture, dont plusieurs programmes de refonte de "
                     "plateformes distribuées à forte volumétrie et supervision."},
        {"skill": "Microsoft Azure", "category": "Cloud", "level": "confirmé",
         "evidence": "Environnement technique Azure."},
        {"skill": "API REST", "category": "Architecture / Développement", "level": "confirmé",
         "evidence": "API REST en .NET."},
        {"skill": "Communication technique avec les directions métier et les équipes produit",
         "category": "Intelligence artificielle & serveurs MCP", "level": "confirmé",
         "evidence": "Restitution orale et écrite auprès des comités de direction."},
    ],
}

# Les trois éléments sont mesurés pour chaque ligne : nom, sélecteur de niveau, catégorie.
MEASURE = """() => {
  const rows = [...document.querySelectorAll('#skillMatrix .matrix-row')];
  const box = el => {const b = el.getBoundingClientRect();
    return {x: b.x, y: b.y, w: b.width, h: b.height, r: b.right, b: b.bottom};};
  return rows.map(row => ({
    row: box(row),
    name: box(row.querySelector('strong')),
    level: box(row.querySelector('select.matrix-level')),
    category: box(row.querySelector('.matrix-category')),
  }));
}"""


def spread(values):
    return max(values) - min(values)


def check(label, ok, detail=""):
    print(("  OK  " if ok else "  KO  ") + label + (f" — {detail}" if detail else ""))
    return bool(ok)


def aligned(measures, key):
    """Toutes les boîtes ``key`` partagent-elles la même abscisse et la même largeur ?"""
    xs = [round(m[key]["x"]) for m in measures]
    ws = [round(m[key]["w"]) for m in measures]
    ok = spread(xs) <= 1 and spread(ws) <= 1
    return ok, f"x de {min(xs)} à {max(xs)} px, largeur de {min(ws)} à {max(ws)} px"


def inside(measures, key):
    """Le contrôle reste-t-il dans la carte de sa ligne (aucun débordement) ?"""
    outs = [m for m in measures if m[key]["x"] < m["row"]["x"] - 1
            or m[key]["r"] > m["row"]["r"] + 1
            or m[key]["y"] < m["row"]["y"] - 1
            or m[key]["b"] > m["row"]["b"] + 1]
    return not outs, f"{len(outs)} ligne(s) hors carte"


def no_overlap(measures):
    """Les cartes ne se chevauchent pas : chaque ligne commence sous la précédente."""
    tops = [m["row"]["y"] for m in measures]
    bottoms = [m["row"]["b"] for m in measures]
    ok = all(tops[i] >= bottoms[i - 1] - 1 for i in range(1, len(tops)))
    return ok, "cartes empilées"


def main():
    failures = []
    r = httpx.put(f"{BASE}/api/profile", timeout=60, json={
        "name": FICHE["name"], "data": FICHE, "validate_profile": False, "source_text": ""})
    failures.append(not check("PUT /api/profile : matrice enregistrée",
                              r.status_code == 200, f"HTTP {r.status_code}"))

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except Exception:
            browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(600)
        page.click("#matrixSummary")
        page.wait_for_timeout(400)
        rows = page.locator("#skillMatrix .matrix-row").count()
        failures.append(not check("matrice affichée", rows == len(FICHE["skill_matrix"]),
                                  f"{rows} ligne(s)"))

# La colonne du CV change de largeur : au-dessus de 1000 px l'encart est étroit
        # (grille à deux colonnes), en dessous il occupe toute la page. Les contrôles
        # doivent rester alignés dans les deux cas.
        for width in (1920, 1600, 1440, 1280, 1100, 1000, 900, 760):
            page.set_viewport_size({"width": width, "height": 1000})
            page.wait_for_timeout(250)
            measures = page.evaluate(MEASURE)
            print(f"largeur {width} px (carte de {round(measures[0]['row']['w'])} px) :")
            ok, detail = aligned(measures, "level")
            failures.append(not check("sélecteurs de niveau alignés", ok, detail))
            ok, detail = aligned(measures, "category")
            failures.append(not check("étiquettes de catégorie alignées", ok, detail))
            ok, detail = inside(measures, "level")
            failures.append(not check("niveau contenu dans sa carte", ok, detail))
            ok, detail = inside(measures, "category")
            failures.append(not check("catégorie contenue dans sa carte", ok, detail))
            ok, detail = no_overlap(measures)
            failures.append(not check("cartes non chevauchées", ok, detail))
            if width in (1440, 1000):
                page.locator(".cv-card").screenshot(path=str(OUT / f"60-matrix-{width}.png"))

        browser.close()

    print("échecs :", sum(1 for f in failures if f))
    return 1 if any(failures) else 0


if __name__ == "__main__":
    sys.exit(main())