"""Sonde UI Playwright (outil manuel, hors suite pytest) : vérifie le rendu desktop.

Pré-requis : `pip install playwright` (réutilise Edge si Chromium n'est pas
téléchargé). Lance d'abord le serveur sur http://127.0.0.1:8000, puis :

    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe tests/_ui_probe.py

Les captures sont écrites dans tests/_shots/ (non versionné).
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = "e:/projets/job-search-system/job-watch/tests/_shots"


def launch(p):
    """Réutilise Edge (pas de téléchargement) puis retombe sur Chromium."""
    try:
        return p.chromium.launch(channel="msedge")
    except Exception:
        return p.chromium.launch()


def main():
    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(BASE, wait_until="networkidle")
        page.click('[data-tab="profile"]')  # atterrissage contextuel : on force l'onglet Profil
        page.wait_for_timeout(400)

        print("aside parent:", page.eval_on_selector(".cv-card", "el => el.parentElement.className"))
        print("profile columns:", page.eval_on_selector(".profile-grid", "el => getComputedStyle(el).gridTemplateColumns"))
        page.screenshot(path=f"{OUT}/40-profile.png", full_page=True)

        page.click('[data-tab="market"]')
        page.wait_for_timeout(1500)
        print("cards:", page.locator("#jobList .job").count(), "| details visibles:", page.locator("#jobList .job-details:visible").count())
        print("summary:", page.locator("#jobsFilterCount").inner_text())
        page.screenshot(path=f"{OUT}/41-market.png")
        card = page.locator("#jobList .job").nth(0)
        card.screenshot(path=f"{OUT}/42-job-card.png")
        card.locator(".details-toggle").click()
        page.wait_for_timeout(300)
        card.screenshot(path=f"{OUT}/43-job-card-details.png")

        page.click('[data-tab="applications"]')
        page.wait_for_timeout(1500)
        print("sticky:", page.eval_on_selector(".dossier-side", "el => getComputedStyle(el).position"))
        print("timeline items:", page.locator(".timeline li").count())
        print("badges:", page.locator(".dossier-badges .badge").all_inner_texts())
        page.screenshot(path=f"{OUT}/44-applications.png", full_page=True)

        page.click('[data-tab="tracking"]')
        page.wait_for_timeout(1200)
        print("kanban dates:", page.locator(".kanban-card .kanban-dates").count())
        page.screenshot(path=f"{OUT}/45-tracking.png")

        # contrôles de robustesse sur les largeurs desktop usuelles
        for width in (1280, 1024):
            page.set_viewport_size({"width": width, "height": 900})
            page.click('[data-tab="market"]')
            page.wait_for_timeout(900)
            page.screenshot(path=f"{OUT}/46-market-{width}.png")
            page.click('[data-tab="applications"]')
            page.wait_for_timeout(900)
            page.screenshot(path=f"{OUT}/47-applications-{width}.png")
            print(f"{width}px -> cartes:", page.locator("#jobList .job").count())
        print("done")
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
