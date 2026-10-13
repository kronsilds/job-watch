"""Sonde UI temporaire : bouton « Ouvrir l'annonce » dans le panneau « Annonce visée ».

Pré-requis : serveur lancé sur http://127.0.0.1:8011.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8011"
OUT = "e:/projets/job-search-system/job-watch/tests/_shots"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{BASE}/static/app.js", wait_until="domcontentloaded")  # sanity
        page.goto(BASE, wait_until="networkidle")

        # 1. Le panneau « Annonce visée » propose-t-il d'ouvrir l'annonce ?
        page.click('[data-tab="applications"]')
        page.wait_for_timeout(800)
        options = page.eval_on_selector_all(
            "#dossierPicker option", "els => els.map(e => e.value).filter(Boolean)")
        print("dossiers:", options)
        page.select_option("#dossierPicker", options[0])
        page.wait_for_timeout(1200)
        link = page.locator(".dossier-job .dossier-panel-head .link-btn")
        print("bouton annonce:", link.count(), "->", link.first.inner_text() if link.count() else None)
        print("href:", link.first.get_attribute("href") if link.count() else None)
        print("target:", link.first.get_attribute("target") if link.count() else None)
        panel = page.locator(".dossier-job")
        panel.screenshot(path=f"{OUT}/50-annonce-visee.png")
        print("panel text head:", page.locator(".dossier-job .dossier-panel-head").inner_text())

        # 2. Explication de note : libellés corrigés selon la provenance des compétences.
        html = page.evaluate(
            "() => scoreBreakdown({explanation:{dimensions:{competences:5.0},weights:{competences:0.4},"
            "competences_source:'absente',gaps:[]}})")
        print("index absent:", "neutralisée" in html, "|", html.count("Compétences demandées") == 0)
        html2 = page.evaluate(
            "() => scoreBreakdown({explanation:{dimensions:{competences:0.0},weights:{competences:0.4},"
            "competences_source:'offre',gaps:['kubernetes']}})")
        print("gaps offre:", "Compétences demandées absentes du profil : kubernetes" in html2)

        # 3. Carte d'offre : « Ouvrir l'annonce » reste présent côté marché.
        page.click('[data-tab="market"]')
        page.wait_for_timeout(1200)
        print("liens marché:", page.locator("#jobList .job .link-btn").count())

        browser.close()


if __name__ == "__main__":
    sys.exit(main())
