"""Événements de progression (SSE) streamés vers le dashboard.

Les opérations longues (import CV, veille/campagne) émettent des trames
``data: {...}`` (Server-Sent Events) pour que l'utilisateur voie l'agent
travailler au lieu d'attendre devant un écran immobile :

* ``{"type": "progress", "step": ..., "message": ...}`` — étape en cours ;
* ``{"type": "done", "result": ...}`` — résultat final (même charge utile
  que les routes historiques non streamées) ;
* ``{"type": "error", "message": ...}`` — erreur bloquante.
"""
import json


class P:
    """Fabrique d'événements."""

    @staticmethod
    def progress(step, message, **extra):
        return {"type": "progress", "step": step, "message": message, **extra}

    @staticmethod
    def done(result):
        return {"type": "done", "result": result}

    @staticmethod
    def error(message):
        return {"type": "error", "message": message}


def sse(event: dict) -> str:
    """Sérialise un événement en trame SSE (``data: ...``)."""
    return "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
