"""Signaux d'alerte affichés sur les cartes d'offres.

Résume en quelques puces ce qui demande l'attention, à partir de données déjà
calculées (fraîcheur, date limite, filtres du scoring, diffusions) :

=============  =======  ===================================================
signal         niveau   condition
=============  =======  ===================================================
``ended``      bad      offre terminée, date limite passée ou ancienne
``blocking``   bad      filtre du scoring non respecté (contrat, expérience…)
``closing``    warn     date limite dans 7 jours au plus
``new``        ok       publiée il y a 3 jours au plus (« Récente »)
``online``     neutral  ancienne mais toujours affichée par l'entreprise
``multi``      neutral  diffusée sur plusieurs sites
=============  =======  ===================================================

Les signaux sont triés par importance ; l'interface affiche les trois
premiers. Chaque puce porte un libellé court et un ``title`` détaillé (icône
jamais seule).
"""

from __future__ import annotations

from datetime import datetime

from .freshness import aware, utcnow

CLOSING_DAYS = 7
NEW_DAYS = 3
MAX_SIGNALS = 3

# Filtres du scoring, du plus au moins décisif pour l'utilisateur.
_GATE_ORDER = ("contrat", "expérience", "localisation", "intitulé", "salaire", "exclusions")


def _days_label(days: int) -> str:
    return "aujourd'hui" if days <= 0 else ("demain" if days == 1 else f"dans {days} j")


def _blocking(gate: str, explanation: dict, contract_label: str, job) -> dict:
    """Puce courte pour un filtre non respecté."""
    xp = explanation.get("experience_required") or {}
    label = {
        "contrat": f"Contrat {contract_label}" if contract_label else "Contrat incompatible",
        "expérience": (f"{xp['min_years']:g} an(s) d'exp. exigés" if xp.get("min_years")
                       else (f"Niveau {xp['level']}" if xp.get("level") else "Expérience")),
        "localisation": "Hors zone",
        "intitulé": "Autre métier",
        "salaire": "Salaire bas",
        "exclusions": "Mot exclu",
    }.get(gate, gate)
    reasons = explanation.get("blocking_reasons") or []
    detail = next((r for r in reasons if gate[:5] in r), "")
    return {"kind": "blocking", "level": "bad", "icon": "alert", "label": label,
            "title": detail or f"Filtre « {gate} » non respecté : note plafonnée"}


def job_signals(job, explanation: dict | None, fresh: dict, sources: list[dict] | None,
                contract_label: str = "", now: datetime | None = None) -> list[dict]:
    """Signaux d'une offre, du plus au moins important (``MAX_SIGNALS`` gardés)."""
    now = aware(now) or utcnow()
    explanation = explanation or {}
    out: list[dict] = []

    if not fresh.get("fresh", True):
        ended = (getattr(job, "availability", "") == "expired"
                 or fresh.get("basis") in ("verification", "expiration"))
        out.append({"kind": "ended", "level": "bad", "icon": "ended",
                    "label": "Terminée" if ended else (
                        f"Ancienne · {fresh['age_days']} j" if fresh.get("age_days") is not None
                        else "Périmée"),
                    "title": fresh.get("reason") or ""})

    gates = explanation.get("gates") or {}
    for gate in _GATE_ORDER:
        if gates.get(gate) is False:
            out.append(_blocking(gate, explanation, contract_label, job))

    exp = aware(getattr(job, "expires_at", None))
    if exp and exp >= now and fresh.get("fresh", True):
        days = (exp.date() - now.date()).days
        if days <= CLOSING_DAYS:
            out.append({"kind": "closing", "level": "warn", "icon": "hourglass",
                        "label": f"Clôture {_days_label(days)}",
                        "title": f"Date limite de candidature : {exp:%d/%m/%Y}"})

    age = fresh.get("age_days")
    if age is not None and age <= NEW_DAYS and fresh.get("fresh", True):
        out.append({"kind": "new", "level": "ok", "icon": "bolt",
                    "label": "Récente · " + ("aujourd'hui" if age <= 0 else f"{age} j"),
                    "title": "Publiée récemment : candidater tôt augmente la visibilité"})

    if fresh.get("basis") == "en_ligne" and str(fresh.get("note") or "").startswith("publiée"):
        out.append({"kind": "online", "level": "neutral", "icon": "check",
                    "label": f"Toujours en ligne · {age} j" if age is not None else "Toujours en ligne",
                    "title": fresh.get("note") or ""})

    sites = {str(s.get("source") or "") for s in (sources or []) if s.get("source")}
    if len(sites) >= 2:
        out.append({"kind": "multi", "level": "neutral", "icon": "stack",
                    "label": f"Vue sur {len(sites)} sites",
                    "title": "Diffusée sur : " + ", ".join(sorted(sites))})
    return out[:MAX_SIGNALS]
