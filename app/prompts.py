"""Centralized, JSON-driven prompt templates and task-tier mapping.

Prompts live in ``app/data/prompts.json`` (not inline in code). Each task
declares a ``tier`` ("heavy" or "light") used to pick the model, plus a
``system`` and ``user`` template.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data"

# Task tiers: which model to use for a given task.
#   heavy -> LLM_MODEL_HEAVY (puissant, lent, pour l'analyse poussée)
#   light -> LLM_MODEL_LIGHT (léger, rapide, pour la rédaction/classification)
SUPPORTED_TASKS = ("analyze_profile", "generate", "generate_cv", "complete_profile", "refine_profile", "market_summary", "market_analysis", "writer_pitch", "writer_cv_ats", "reviewer")


def _read(name: str) -> dict:
    with (_DATA / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def load_prompts() -> dict:
    return _read("prompts.json")


def load_keywords() -> dict:
    return _read("keywords.json")


def _to_text(value) -> str:
    """Serialize values for injection into a prompt (dict/list -> JSON)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


class PromptRenderer:
    """Loads prompt templates once and substitutes only known placeholders.

    Substitution is done with ``str.replace`` (not ``str.format``) so that
    braces in the templates (e.g. the JSON schema example) or in user content
    never raise errors.
    """

    def __init__(self, data: dict):
        self.data = data

    def tier_for(self, task: str) -> str:
        return self.data.get(task, {}).get("tier", "heavy")

    def render(self, task: str, **kwargs) -> tuple[str, str]:
        block = self.data.get(task)
        if not block:
            raise KeyError(f"Prompt inconnu : {task!r}")
        system = block["system"]
        user = block["user"]
        for key, value in kwargs.items():
            token = "{" + key + "}"
            text = _to_text(value)
            system = system.replace(token, text)
            user = user.replace(token, text)
        return system, user


prompts = PromptRenderer(load_prompts())