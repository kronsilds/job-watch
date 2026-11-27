"""Traçage Langfuse optionnel des appels LLM.

Actif seulement si ``LANGFUSE_PUBLIC_KEY`` et ``LANGFUSE_SECRET_KEY`` sont
définies (et ``LANGFUSE_TRACING_ENABLED`` n'est pas ``false``) et que le SDK
``langfuse`` (v5) est installé. Sinon tout est no-op : l'application et les
tests tournent à l'identique, sans réseau.

Structure des traces :

- une observation racine par tâche (``analyze-profile``, ``enrich-offer``,
  ``refine-document``…) dont l'input/output résume la tâche ;
- une ``generation`` par appel modèle (modèle, paramètres, tokens, transport
  SDK/HTTP), y compris les nouvelles tentatives et les échecs ;
- le repli déterministe est visible (niveau ``WARNING`` + ``metadata``).

Les CV et annonces contiennent des données personnelles : les e-mails et
numéros de téléphone sont masqués à l'export (``mask_otel_spans``).
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

# Identifiant de l'utilisateur job-watch courant (posé par le middleware HTTP).
_current_user_id: ContextVar[str | None] = ContextVar(
    "jobwatch_langfuse_user_id", default=None)

_client: Any = None
_init_failed = False

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Numéros français (+33 / 0033 / 0X) et internationaux avec séparateurs.
_PHONE_RE = re.compile(
    r"(?<![\w])(?:(?:\+|00)\d{2,3}[\s.-]?(?:\(0\))?[\s.-]?\d|0[1-9])"
    r"(?:[\s.-]?\d{2}){4}(?!\d)")


def mask_text(text: str) -> str:
    """Remplace e-mails et numéros de téléphone par des marqueurs."""
    text = _EMAIL_RE.sub("[EMAIL]", text)
    return _PHONE_RE.sub("[TELEPHONE]", text)


def _mask_otel_spans(*, params):
    """Hook d'export Langfuse : masque les PII dans tous les attributs texte."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        changed = {}
        for key, value in span.attributes.items():
            if isinstance(value, str):
                masked = mask_text(value)
                if masked != value:
                    changed[key] = masked
        if changed:
            patches[identifier] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches) if patches else None


def enabled() -> bool:
    """Traçage actif (clés présentes, SDK importable, pas désactivé)."""
    if _init_failed:
        return False
    if os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() in ("0", "false", "no"):
        return False
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def client():
    """Client Langfuse singleton (``None`` si le traçage est inactif)."""
    global _client, _init_failed
    if _client is not None or not enabled():
        return _client
    try:
        from langfuse import Langfuse
        os.environ.setdefault("OTEL_SERVICE_NAME", "job-watch")
        # Le constructeur enregistre le singleton renvoyé ensuite par get_client().
        _client = Langfuse(mask_otel_spans=_mask_otel_spans)
    except Exception as exc:  # SDK absent ou configuration invalide
        print(f"[Langfuse] traçage désactivé : {exc}")
        _init_failed = True
        _client = None
    return _client


def set_user(user_id: Any) -> None:
    """Associe les traces de la requête courante à un compte job-watch."""
    _current_user_id.set(str(user_id) if user_id is not None else None)


class _NoopObservation:
    def update(self, **_: Any) -> "_NoopObservation":
        return self


_NOOP = _NoopObservation()


@contextmanager
def observe(name: str, *, as_type: str = "span", session_id: str | None = None,
            tags: list[str] | None = None, **fields: Any) -> Iterator[Any]:
    """Ouvre une observation Langfuse active (enfant de l'observation courante).

    ``fields`` : ``input``, ``output``, ``metadata``, ``model``,
    ``model_parameters``… (cf. ``start_as_current_observation``). Une
    exception marque l'observation en ``ERROR`` puis est propagée.
    """
    lf = client()
    if lf is None:
        yield _NOOP
        return
    from langfuse import propagate_attributes

    with lf.start_as_current_observation(name=name, as_type=as_type, **fields) as obs:
        attrs: dict[str, Any] = {}
        user_id = _current_user_id.get()
        if user_id:
            attrs["user_id"] = user_id
        if session_id:
            attrs["session_id"] = session_id
        if tags:
            attrs["tags"] = tags
        with propagate_attributes(**attrs):
            try:
                yield obs
            except Exception as exc:
                obs.update(level="ERROR", status_message=str(exc)[:500])
                raise


def update_generation(**fields: Any) -> None:
    """Complète la ``generation`` active (usage, transport…) ; no-op sinon."""
    lf = client()
    if lf is not None:
        try:
            lf.update_current_generation(**fields)
        except Exception:
            pass


def record_response(kind: str, transport: str, response: Any) -> None:
    """Usage + métadonnées d'une réponse ``openai``/``anthropic`` (objet SDK ou
    JSON HTTP) sur la generation active. Ne lève jamais : le traçage ne doit
    pas modifier le chemin d'appel."""
    if client() is None:
        return
    try:
        get = (lambda k: response.get(k) if isinstance(response, dict)
               else getattr(response, k, None))
        usage = get("usage")
        metadata = {"transport": transport, "response_model": get("model")}
        if kind == "anthropic":
            metadata["stop_reason"] = get("stop_reason")
        fields: dict[str, Any] = {"metadata": metadata,
                                  "output": _assistant_message(kind, response)}
        if usage:
            fields["usage_details"] = (anthropic_usage(usage) if kind == "anthropic"
                                       else openai_usage(usage))
        update_generation(**fields)
    except Exception:
        pass


def _field(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _assistant_message(kind: str, response: Any) -> dict[str, Any]:
    """Réponse brute du modèle en message ``assistant`` (avant parsing JSON),
    avec le raisonnement (« thinking ») quand l'API l'expose."""
    if kind == "anthropic":
        blocks = _field(response, "content") or []
        text = "".join(str(_field(b, "text") or "") for b in blocks
                       if _field(b, "type") == "text")
        thinking = "\n".join(str(_field(b, "thinking") or "") for b in blocks
                             if _field(b, "type") == "thinking")
    else:
        choices = _field(response, "choices") or []
        message = _field(choices[0], "message") if choices else None
        text = _field(message, "content") or ""
        # DeepSeek / OpenRouter : reasoning_content ou reasoning.
        thinking = (_field(message, "reasoning_content")
                    or _field(message, "reasoning") or "")
    out: dict[str, Any] = {"role": "assistant", "content": text}
    if thinking:
        out["reasoning"] = thinking
    return out


def openai_usage(usage: Any) -> dict[str, int]:
    """Usage OpenAI (compteurs inclusifs) → buckets Langfuse exclusifs."""
    get = (lambda o, k: (o or {}).get(k) if isinstance(o, dict) else getattr(o, k, None))
    prompt = int(get(usage, "prompt_tokens") or 0)
    completion = int(get(usage, "completion_tokens") or 0)
    cached = int(get(get(usage, "prompt_tokens_details"), "cached_tokens") or 0)
    reasoning = int(get(get(usage, "completion_tokens_details"), "reasoning_tokens") or 0)
    details = {"input": max(prompt - cached, 0), "output": max(completion - reasoning, 0)}
    if cached:
        details["input_cached_tokens"] = cached
    if reasoning:
        details["output_reasoning_tokens"] = reasoning
    return details


def anthropic_usage(usage: Any) -> dict[str, int]:
    """Usage Anthropic (déjà exclusif : ``input_tokens`` hors cache)."""
    get = (lambda k: usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None))
    details = {"input": int(get("input_tokens") or 0),
               "output": int(get("output_tokens") or 0)}
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if get(key):
            details[key] = int(get(key))
    return details


def flush() -> None:
    if _client is not None:
        _client.flush()


def shutdown() -> None:
    if _client is not None:
        _client.shutdown()
