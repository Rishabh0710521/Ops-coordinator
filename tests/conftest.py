from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import config, db, llm, orchestrator
from app.main import app

SESSION_A = "session-aaaaaaaa"
SESSION_B = "session-bbbbbbbb"

KNOWN_DATES = {
    "two weeks": "2026-11-13",
    "Friday 13 November": "2026-11-13",
    "Friday 20 November": "2026-11-20",
}


class FakeModel:
    def __init__(self):
        self.calls = []
        self.stubborn = False
        self.invent_owner = False
        self.queue = []

    def __call__(self, system, payload, max_tokens=3500):
        if self.queue:
            agent, out = self.queue.pop(0)
            self.calls.append(agent)
            return out, {"in": 100, "out": 50}
        if system.startswith("You are the Intake Agent"):
            self.calls.append("intake")
            out = self.intake(payload)
        elif system.startswith("You are the Planning Agent"):
            self.calls.append("planning")
            out = self.planning(payload)
        else:
            self.calls.append("review")
            out = {"verdict": "pass", "issues": [], "notes": "nothing further"}
        return out, {"in": 100, "out": 50}

    def intake(self, payload):
        facts = [
            {"id": "F1", "kind": "decision", "text": "Arjun builds the points ledger", "source_refs": ["L2"],
             "owner": "Arjun", "deadline_text": "two weeks"},
            {"id": "F2", "kind": "decision", "text": "Design handoff is due Friday 13 November",
             "source_refs": ["L5"], "owner": "Meera", "deadline_text": "Friday 13 November"},
            {"id": "F3", "kind": "constraint", "text": "Security review must finish before launch",
             "source_refs": ["L13", "L14"]},
        ]
        if self.invent_owner:
            facts.append({"id": "F4", "kind": "decision", "text": "Reporting is set up", "source_refs": ["L2"],
                          "owner": "Zed", "deadline_text": "tomorrow"})
        flags = [
            {"id": "X1", "kind": "conflict", "text": "Launch date is 4 December in one line and 11 December in another",
             "source_refs": ["L3", "L7"], "related_fact_ids": []},
            {"id": "X2", "kind": "missing", "text": "Nobody owns store staff training",
             "source_refs": ["L10"], "related_fact_ids": []},
        ]
        return {"facts": facts, "flags": flags}

    def planning(self, payload):
        facts = {f["id"]: f for f in payload["facts"]}
        f1, f2 = facts["F1"], facts["F2"]
        tasks = [
            {"id": "T1", "title": "Build the points ledger", "owner": f1.get("owner"),
             "owner_basis": "stated", "deadline": KNOWN_DATES.get(f1.get("deadline_text")),
             "deadline_basis": "stated", "depends_on": [], "supported_by": ["F1"], "basis": "supported",
             "rationale": "Arjun said he owns it"},
            {"id": "T2", "title": "Deliver the design handoff", "owner": f2.get("owner"),
             "owner_basis": "stated", "deadline": KNOWN_DATES.get(f2.get("deadline_text")),
             "deadline_basis": "stated", "depends_on": [], "supported_by": ["F2"], "basis": "supported",
             "rationale": "Meera committed to the date"},
            {"id": "T3", "title": "Run the security review", "owner": "Sana", "owner_basis": "recommended",
             "deadline": "2026-12-02", "deadline_basis": "recommended", "depends_on": ["T1", "T2"],
             "supported_by": ["F3"], "basis": "recommendation", "rationale": "Policy gate before launch"},
            {"id": "T4", "title": "Launch the pilot", "owner": "Priya", "owner_basis": "recommended",
             "deadline": "2026-12-04", "deadline_basis": "recommended", "depends_on": ["T3"],
             "supported_by": ["F3"], "basis": "recommendation", "rationale": "Follows the review"},
        ]
        changes = []
        for issue in payload.get("review_feedback", []):
            task = next((t for t in tasks if t["id"] == issue.get("task_id")), None)
            if not task:
                continue
            if issue.get("rule_id") == "R1":
                task["owner"] = "Arjun"
                changes.append(f"{task['id']} owner moved back to a roster member")
            if issue.get("rule_id") == "R3":
                day = datetime.strptime(task["deadline"], "%Y-%m-%d")
                task["deadline"] = (day - timedelta(days=max(day.weekday() - 4, 0))).strftime("%Y-%m-%d")
                changes.append(f"{task['id']} deadline moved to a working day")
        if self.stubborn:
            tasks[0]["owner"] = "Rahul (contractor)"
        questions = [{"question": f["text"], "related": [f["id"]]} for f in payload["open_flags"]]
        return {"summary": "Plan for the pilot", "tasks": tasks, "unresolved_questions": questions,
                "changes_made": changes}


@pytest.fixture
def fake(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(llm, "complete_json", model)
    return model


@pytest.fixture
def client(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "MAX_REVIEW_ROUNDS", 3)
    monkeypatch.setattr(orchestrator, "SYNC", True)
    db.init()
    with TestClient(app) as test_client:
        yield test_client


def headers(sid=SESSION_A):
    return {"X-Session-Id": sid}


@pytest.fixture
def start(client):
    sample = client.get("/api/sample").json()

    def make(faults=(), sid=SESSION_A):
        res = client.post(
            "/api/runs",
            json={"transcript": sample["transcript"], "rules": sample["rules"], "faults": list(faults)},
            headers=headers(sid),
        )
        assert res.status_code == 201, res.text
        run_id = res.json()["id"]
        return run_id, client.get(f"/api/runs/{run_id}", headers=headers(sid)).json()

    return make
