"""Passe de complétion des champs manquants d'une annonce (heuristique + LLM light).

Aucun appel réseau : le chemin LLM est testé avec un provider factice injecté,
et les tests d'orchestration forcent ``LLM_PROVIDER=local`` ou
``JOBWATCH_ENRICH_LLM=0``.
"""

from app.offer_enrichment import (
    enrich_offer, enrich_offers, heuristic_completion, fill_gaps, missing_fields,
    needs_completion, llm_enabled,
)
from app.llm import DeterministicProvider
from app.prompts import prompts as prompt_renderer


class FakeProvider:
    """Provider factice : renvoie une charge utile fixe, sans réseau."""

    name = "fake"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def enrich_offer(self, offer):
        self.calls += 1
        return dict(self.payload)


def test_prompt_registered_as_light_task():
    assert prompt_renderer.tier_for("enrich_offer") == "light"
    system, user = prompt_renderer.render(
        "enrich_offer", offer_text="Poste Python à Lyon", known_fields="{}")
    assert "n'invente" in system.lower()
    assert "Poste Python à Lyon" in user and "{}" in user


def test_heuristic_fills_contract_remote_location():
    off = {"title": "Data Engineer",
           "description": "Poste en CDI, télétravail partiel possible, basé à Lyon."}
    out = heuristic_completion(off)
    assert out["contract"] == "cdi"
    assert "remote" in out["remote"]
    assert out["location"] == "Lyon"


def test_heuristic_detects_salary_range():
    off = {"title": "Dev", "description": "Rémunération : 45K€ - 55K€ selon profil."}
    out = heuristic_completion(off)
    assert out["salary_min"] == 45000 and out["salary_max"] == 55000


def test_heuristic_ignores_salary_without_context():
    # « 100k vues » n'est pas un salaire : pas de signe € ni contexte salarial.
    off = {"title": "Community Manager", "description": "Faire grandir nos réseaux (100k vues/mois)."}
    out = heuristic_completion(off)
    assert out.get("salary_min") is None and out.get("salary_max") is None


def test_heuristic_never_overwrites_scraped_fields():
    off = {"contract": "CDI Temps plein", "location": "Paris 02 - 75",
           "description": "Offre en CDD à Nantes."}
    out = heuristic_completion(off)
    assert out["contract"] == "CDI Temps plein"
    assert out["location"] == "Paris 02 - 75"


def test_time_modality_is_not_a_contract():
    out = heuristic_completion({"title": "Analyste", "description": "Temps plein, horaires flexibles."})
    assert not out.get("contract")


def test_llm_light_fills_only_missing_fields():
    fake = FakeProvider({"contract": "cdd", "skills": ["python", "pytorch"],
                         "salary_max": 60000, "location": "Lyon"})
    off = {"title": "ML Engineer", "contract": "CDI", "description": ""}
    out = enrich_offer(off, provider=fake)
    assert out["contract"] == "CDI"  # champ scrapé : jamais écrasé
    assert out["skills"] == ["python", "pytorch"]
    assert out["salary_max"] == 60000
    assert out["location"] == "Lyon"
    assert fake.calls == 1


def test_enrich_offer_skips_llm_when_complete():
    fake = FakeProvider({"skills": ["rust"]})
    off = {"title": "Dev", "contract": "CDI", "location": "Paris",
           "skills": ["python"], "description": ""}
    out = enrich_offer(off, provider=fake)
    assert fake.calls == 0 and out["skills"] == ["python"]


def test_enrich_offer_without_llm_configured(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")  # provider déterministe, pas de réseau
    out = enrich_offer({"title": "Dev backend", "description": "Poste en CDI à Bordeaux."})
    assert out["contract"] == "cdi" and out["location"] == "Bordeaux"


def test_enrich_offer_llm_failure_falls_back_silently():
    class Boom:
        def enrich_offer(self, offer):
            raise RuntimeError("api down")

    out = enrich_offer({"title": "Dev", "description": "CDI à Nantes."}, provider=Boom())
    assert out["contract"] == "cdi" and out["location"] == "Nantes"


def test_enrich_offer_disabled_by_env(monkeypatch):
    monkeypatch.setenv("JOBWATCH_ENRICH_LLM", "0")
    fake = FakeProvider({"contract": "cdi"})
    out = enrich_offer({"title": "Dev", "description": ""}, provider=fake)
    assert fake.calls == 0 and not out.get("contract")


def test_enrich_offers_caps_remote_calls():
    fake = FakeProvider({"contract": "cdi"})
    records = [{"title": f"Offre {i}", "description": ""} for i in range(5)]
    out = enrich_offers(records, provider=fake, llm_budget=2)
    assert fake.calls == 2
    assert [r.get("contract") for r in out] == ["cdi", "cdi", None, None, None]


def test_llm_salary_out_of_range_rejected():
    fake = FakeProvider({"salary_max": 5})  # absurde -> rejeté par le sanitizer
    out = enrich_offer({"title": "Dev", "description": ""}, provider=fake)
    assert out.get("salary_max") is None


def test_deterministic_provider_enrich_offer_is_heuristic():
    out = DeterministicProvider().enrich_offer(
        {"title": "Dev", "description": "CDI en remote à Lille."})
    assert out["contract"] == "cdi" and out["location"] == "Lille"


def test_missing_fields_and_needs_completion_helpers():
    complete = {"title": "Dev", "company": "Acme", "contract": "CDI",
                "location": "Paris", "skills": ["python"], "salary_max": 50000}
    assert not needs_completion(complete)
    assert missing_fields({}) == ["title", "company", "location", "remote", "contract",
                                  "skills", "sectors", "salary_min", "salary_max"]
