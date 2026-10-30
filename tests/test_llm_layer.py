import json

import httpx
import pytest

from app import agents, config, llm
from app.schemas import IntakeOutput


class Reply:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def good_reply(content):
    return Reply(200, {"choices": [{"message": {"content": json.dumps(content)}}],
                       "usage": {"prompt_tokens": 7, "completion_tokens": 3}})


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)


def test_transient_errors_are_retried(wired, monkeypatch):
    replies = [Reply(429), Reply(503), good_reply({"ok": True})]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: replies.pop(0))
    out, usage = llm.complete_json("sys", {"a": 1})
    assert out == {"ok": True} and usage == {"in": 7, "out": 3}
    assert replies == []


def test_client_errors_are_not_retried(wired, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1) or Reply(401, {"error": "bad key"}))
    with pytest.raises(llm.LLMError, match="401"):
        llm.complete_json("sys", {})
    assert len(calls) == 1


def test_network_failure_surfaces_after_three_tries(wired, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise httpx.ConnectTimeout("slow")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(llm.LLMError, match="network error"):
        llm.complete_json("sys", {})
    assert len(calls) == 3


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    with pytest.raises(llm.LLMError, match="LLM_API_KEY"):
        llm.complete_json("sys", {})


def test_schema_violation_gets_one_repair_attempt(monkeypatch):
    seen = []
    answers = [
        {"facts": [{"id": "F1", "kind": "opinion", "text": "x"}]},
        {"facts": [{"id": "F1", "kind": "decision", "text": "x", "source_refs": ["L1"]}]},
    ]

    def fake(system, payload, max_tokens=3500):
        seen.append(payload)
        return answers.pop(0), {"in": 1, "out": 1}

    monkeypatch.setattr(llm, "complete_json", fake)
    out, usage = agents.ask("sys", {"transcript": []}, IntakeOutput, agents.check_intake)
    assert out.facts[0].kind == "decision"
    assert "fix_these_problems" in seen[1]
    assert usage == {"in": 2, "out": 2}


def test_second_bad_answer_fails_the_step(monkeypatch):
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: ({"facts": []}, {"in": 1, "out": 1}))
    with pytest.raises(agents.AgentError, match="facts is empty"):
        agents.ask("sys", {}, IntakeOutput, agents.check_intake)


def test_plan_grounding_downgrades_unsupported_claims():
    from app.schemas import PlanOutput, Task

    plan = PlanOutput(tasks=[
        Task(id="T1", title="x", owner="Karan", owner_basis="stated", deadline="2026-11-20",
             deadline_basis="stated", supported_by=["F1", "F404"], basis="supported"),
        Task(id="T2", title="y", basis="supported", supported_by=["F404"]),
    ])
    facts = [{"id": "F1", "owner": "Arjun"}]
    notes = agents.ground_plan(plan, facts)
    t1, t2 = plan.tasks
    assert t1.owner_basis == "recommended" and t1.deadline_basis == "recommended"
    assert t1.supported_by == ["F1"]
    assert t2.basis == "recommendation"
    assert len(notes) == 3
