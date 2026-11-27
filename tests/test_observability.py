"""Traçage Langfuse : arbre d'observations, usage, repli, masquage des PII.

Les spans sont exportés vers un exporter OpenTelemetry en mémoire : aucun
appel réseau, aucune clé Langfuse réelle.
"""

import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import observability as obs
from app.llm import DeterministicProvider, FallbackProvider, RemoteProvider


@pytest.fixture(scope="module")
def _pipeline():
    # Langfuse garde un pipeline d'export par clé publique : un seul client
    # (et un seul exporter) pour tout le module.
    from langfuse import Langfuse

    exporter = InMemorySpanExporter()
    client = Langfuse(public_key="pk-lf-test", secret_key="sk-lf-test",
                      base_url="http://127.0.0.1:9", span_exporter=exporter,
                      mask_otel_spans=obs._mask_otel_spans)
    return client, exporter


@pytest.fixture
def spans(monkeypatch, _pipeline):
    client, exporter = _pipeline
    client.flush()
    exporter.clear()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setattr(obs, "_client", client)

    def finished():
        client.flush()
        return {s.name: s for s in exporter.get_finished_spans()}

    yield finished
    obs.set_user(None)


class FakeRemote(RemoteProvider):
    provider_key = "fake"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def _complete(self, system, user, model, temperature):
        if self.fail:
            raise RuntimeError("quota dépassé")
        obs.update_generation(usage_details={"input": 120, "output": 30},
                              metadata={"transport": "sdk"})
        self.calls += 1
        if self.calls % 2 == 0:  # graphe : rédacteur puis évaluateur
            return {"score": 8, "feedback": "ok", "criteria": {}}
        return {"document": "Pitch pour Data Engineer chez Acme",
                "changes_summary": "première version"}


def _attr(span, key):
    value = span.attributes.get(key)
    return json.loads(value) if isinstance(value, str) and value[:1] in "{[\"" else value


def test_mask_text_redacts_contact_details_only():
    text = "jean.dupont@mail.fr, +33 6 12 34 56 78, 0612345678 — 45 000 €, 2019-2023"
    masked = obs.mask_text(text)
    assert "dupont" not in masked and "12 34" not in masked and "0612" not in masked
    assert "45 000 €" in masked and "2019-2023" in masked


def test_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(obs, "_client", None)
    assert not obs.enabled()
    with obs.observe("noop") as span:
        span.update(output="ignored")
    obs.update_generation(usage_details={"input": 1})


def test_task_span_wraps_generation_with_usage(spans):
    obs.set_user(7)
    provider = FallbackProvider(FakeRemote(), DeterministicProvider())
    provider.complete_task("writer_pitch", job_offer={"title": "Data Engineer"},
                           candidate_profile={"email": "a.b@c.fr"},
                           current_draft="", feedback="", user_comments="")
    done = spans()
    root, gen = done["write-pitch"], done["write-pitch-llm"]
    assert gen.parent.span_id == root.context.span_id
    assert _attr(gen, "langfuse.observation.type") == "generation"
    assert _attr(gen, "langfuse.observation.usage_details") == {"input": 120, "output": 30}
    assert gen.attributes["user.id"] == "7"
    assert "a.b@c.fr" not in root.attributes["langfuse.observation.input"]


def test_fallback_is_flagged(spans):
    provider = FallbackProvider(FakeRemote(fail=True), DeterministicProvider())
    result = provider.generate("", {"job_title": "Dev", "company": "Acme"})
    assert "Dev" in result["pitch"]
    done = spans()
    root, gen = done["generate-pitch"], done["generate-pitch-llm"]
    assert root.attributes["langfuse.observation.level"] == "WARNING"
    assert _attr(root, "langfuse.observation.metadata.fallback") == "deterministic"
    assert gen.attributes["langfuse.observation.level"] == "ERROR"


def test_refinement_graph_nests_under_agent_session(spans):
    from app.refinement import _invoke_traced, build_graph

    provider = FallbackProvider(FakeRemote(), DeterministicProvider())
    graph = build_graph(provider=provider, save_fn=lambda s: None)
    state = {"application_id": 5, "document_type": "pitch",
             "job_offer": {"title": "Data Engineer", "company": "Acme"},
             "candidate_profile": {}, "iteration_count": 0, "history": [],
             "warnings": []}
    final = _invoke_traced(graph, state)
    assert final["status"] == "validated"
    done = spans()
    agent = done["refine-document"]
    assert _attr(agent, "langfuse.observation.type") == "agent"
    assert agent.attributes["session.id"] == "application-5"
    for name in ("write-pitch", "review-draft"):
        assert done[name].parent.span_id == agent.context.span_id
        assert done[f"{name}-llm"].attributes["session.id"] == "application-5"


def test_usage_buckets_are_exclusive():
    openai = {"prompt_tokens": 17903, "completion_tokens": 188,
              "prompt_tokens_details": {"cached_tokens": 17817}}
    assert obs.openai_usage(openai) == {"input": 86, "output": 188,
                                        "input_cached_tokens": 17817}
    anthropic = {"input_tokens": 50, "output_tokens": 20, "cache_read_input_tokens": 900}
    assert obs.anthropic_usage(anthropic) == {"input": 50, "output": 20,
                                              "cache_read_input_tokens": 900}


def test_assistant_message_keeps_raw_reply_and_reasoning():
    openai = {"choices": [{"message": {"role": "assistant", "content": '{"n": 51}',
                                       "reasoning_content": "17*3 = 51"}}]}
    assert obs._assistant_message("openai", openai) == {
        "role": "assistant", "content": '{"n": 51}', "reasoning": "17*3 = 51"}
    anthropic = {"content": [{"type": "thinking", "thinking": "réfléchir"},
                             {"type": "text", "text": '{"ok": true}'}]}
    assert obs._assistant_message("anthropic", anthropic) == {
        "role": "assistant", "content": '{"ok": true}', "reasoning": "réfléchir"}
