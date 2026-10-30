from app import orchestrator

from .conftest import SESSION_A, SESSION_B, headers


def steps_of(detail, agent):
    return [s for s in detail["steps"] if s["agent"] == agent]


def fetch(client, run_id, sid=SESSION_A):
    return client.get(f"/api/runs/{run_id}", headers=headers(sid)).json()


def test_complete_three_agent_run(start):
    run_id, d = start()
    assert d["run"]["status"] == "complete"
    assert {a: d["agents"][a]["status"] for a in d["agents"]} == {
        "intake": "done", "planning": "done", "review": "done"}
    kinds = [(h["from"], h["to"], h["kind"]) for h in d["handoffs"]]
    assert ("intake", "planning", "facts") in kinds
    assert ("planning", "review", "plan") in kinds
    assert all(h["valid"] for h in d["handoffs"])
    assert len(d["result"]["plan"]["tasks"]) == 4
    assert d["cost"]["tokens_in"] == 300
    assert d["context"]["version"] == 1


def test_supported_and_recommended_are_labelled(start):
    _, d = start()
    tasks = {t["id"]: t for t in d["result"]["plan"]["tasks"]}
    assert tasks["T1"]["basis"] == "supported" and tasks["T1"]["owner_basis"] == "stated"
    assert tasks["T3"]["basis"] == "recommendation" and tasks["T3"]["owner_basis"] == "recommended"
    assert [q["related"] for q in d["result"]["plan"]["unresolved_questions"]] == [["X1"], ["X2"]]


def test_rule_violation_is_sent_back_and_fixed(start):
    _, d = start(["violation:planning"])
    assert d["run"]["status"] == "complete"
    first, second = steps_of(d, "review")
    assert first["output"]["verdict"] == "fail"
    rules_hit = {i["rule_id"] for i in first["output"]["issues"]}
    assert {"R1", "R3"} <= rules_hit
    assert all(i["source"] == "rule-engine" for i in first["output"]["issues"])
    assert second["output"]["verdict"] == "pass"
    feedback = [h for h in d["handoffs"] if h["kind"] == "feedback"]
    assert len(feedback) == 1 and feedback[0]["from"] == "review" and feedback[0]["to"] == "planning"
    planning = steps_of(d, "planning")
    assert len(planning) == 2
    assert planning[0]["simulated"] is True
    assert any("SIMULATED FAULT" in n for n in planning[0]["notes"])
    assert planning[1]["input"]["review_feedback"]
    assert d["result"]["unresolved_issues"] == []
    assert d["result"]["rounds"] == 2


def test_review_loop_is_bounded(start, fake):
    fake.stubborn = True
    _, d = start()
    assert d["run"]["status"] == "complete_with_issues"
    assert d["result"]["rounds"] == 3
    assert len(steps_of(d, "planning")) == 3
    assert d["result"]["unresolved_issues"]
    assert "Stopped after 3 review rounds" in d["run"]["note"]


def test_changed_fact_propagates_without_stale_context(client, start, fake):
    run_id, before = start()
    assert before["result"]["plan"]["tasks"][1]["deadline"] == "2026-11-13"
    res = client.post(f"/api/runs/{run_id}/corrections",
                      json={"fact_id": "F2", "deadline_text": "Friday 20 November"}, headers=headers())
    assert res.status_code == 200 and res.json()["context_version"] == 2
    d = fetch(client, run_id)
    assert d["run"]["status"] == "complete"
    assert d["context"]["version"] == 2
    tasks = {t["id"]: t for t in d["result"]["plan"]["tasks"]}
    assert tasks["T2"]["deadline"] == "2026-11-20"
    assert fake.calls.count("intake") == 1
    assert len(steps_of(d, "intake")) == 1
    assert len(steps_of(d, "planning")) == 2
    newest_input = steps_of(d, "planning")[-1]["input"]
    f2 = next(f for f in newest_input["facts"] if f["id"] == "F2")
    assert f2["deadline_text"] == "Friday 20 November" and f2["corrected"] is True
    assert d["propagation"]["affected_tasks"]["F2"] == ["T2"]
    assert {"task_id": "T2", "field": "deadline", "before": "2026-11-13", "after": "2026-11-20"} in d["propagation"]["changes"]
    undone = client.post(f"/api/runs/{run_id}/corrections",
                         json={"fact_id": "F2", "revert": True}, headers=headers())
    assert undone.json()["context_version"] == 3
    assert fetch(client, run_id)["result"]["plan"]["tasks"][1]["deadline"] == "2026-11-13"


def test_answering_a_flag_reaches_planning(client, start):
    run_id, _ = start()
    res = client.post(f"/api/runs/{run_id}/resolutions",
                      json={"flag_id": "X1", "answer": "Launch is 4 December"}, headers=headers())
    assert res.status_code == 200
    d = fetch(client, run_id)
    assert d["context"]["resolutions"] == {"X1": "Launch is 4 December"}
    assert [f["id"] for f in d["result"]["open_flags"]] == ["X2"]
    assert d["steps"][-1]["agent"] == "review"
    planning_input = steps_of(d, "planning")[-1]["input"]
    assert planning_input["resolutions"][0]["answer"] == "Launch is 4 December"


def test_model_failure_then_resume_does_not_repeat_finished_work(client, start, fake):
    run_id, d = start(["fail:planning"])
    assert d["run"]["status"] == "failed"
    assert d["run"]["failed_step"] == "planning"
    assert "SIMULATED" in d["run"]["note"]
    failed = steps_of(d, "planning")[0]
    assert failed["status"] == "failed" and failed["simulated"] is True
    assert d["agents"]["intake"]["status"] == "done"
    assert fake.calls == ["intake"]

    assert client.post(f"/api/runs/{run_id}/resume", headers=headers()).status_code == 200
    d = fetch(client, run_id)
    assert d["run"]["status"] == "complete"
    assert fake.calls == ["intake", "planning", "review"]
    assert len(steps_of(d, "intake")) == 1
    planning = steps_of(d, "planning")
    assert len(planning) == 1 and planning[0]["retries"] == 1
    assert d["cost"]["tokens_in"] == 300
    assert client.post(f"/api/runs/{run_id}/resume", headers=headers()).status_code == 409


def test_failure_in_review_keeps_the_saved_plan(client, start, fake):
    run_id, d = start(["fail:review"])
    assert d["run"]["failed_step"] == "review"
    client.post(f"/api/runs/{run_id}/resume", headers=headers())
    assert fake.calls == ["intake", "planning", "review"]


def test_sessions_are_isolated(client, start):
    run_id, _ = start(sid=SESSION_A)
    assert client.get(f"/api/runs/{run_id}", headers=headers(SESSION_B)).status_code == 404
    assert client.get("/api/runs", headers=headers(SESSION_B)).json() == []
    assert client.post(f"/api/runs/{run_id}/resume", headers=headers(SESSION_B)).status_code == 404
    body = {"fact_id": "F1", "owner": "Ishaan"}
    assert client.post(f"/api/runs/{run_id}/corrections", json=body, headers=headers(SESSION_B)).status_code == 404
    other_id, _ = start(sid=SESSION_B)
    assert [r["id"] for r in client.get("/api/runs", headers=headers(SESSION_A)).json()] == [run_id]
    assert [r["id"] for r in client.get("/api/runs", headers=headers(SESSION_B)).json()] == [other_id]
    assert client.get("/api/runs").status_code == 400


def test_reset_only_touches_the_callers_session(client, start):
    start(sid=SESSION_A)
    start(sid=SESSION_A)
    other_id, _ = start(sid=SESSION_B)
    assert client.post("/api/reset", headers=headers(SESSION_A)).json()["removed"] == 2
    assert client.get("/api/runs", headers=headers(SESSION_A)).json() == []
    assert [r["id"] for r in client.get("/api/runs", headers=headers(SESSION_B)).json()] == [other_id]


def test_invented_owner_is_stripped_by_grounding(start, fake):
    fake.invent_owner = True
    _, d = start()
    f4 = next(f for f in d["result"]["facts"] if f["id"] == "F4")
    assert f4["owner"] is None and f4["deadline_text"] is None
    notes = steps_of(d, "intake")[0]["notes"]
    assert any("owner 'Zed'" in n for n in notes)


def test_a_run_cannot_be_started_twice(client):
    run_id = orchestrator.create_run(SESSION_A, "x" * 50, {"roster": [], "params": {}, "rules": []}, [], "t")
    assert orchestrator.claim(run_id, ("queued",)) is True
    assert orchestrator.claim(run_id, ("queued",)) is False
    assert orchestrator.claim(run_id, ("failed",)) is False


def test_changes_are_refused_while_running(client, start):
    run_id, _ = start()
    orchestrator.claim(run_id, ("complete",))
    res = client.post(f"/api/runs/{run_id}/corrections", json={"fact_id": "F1", "owner": "Ishaan"}, headers=headers())
    assert res.status_code == 409


def test_bad_input_is_rejected(client, start):
    run_id, _ = start()
    unknown = client.post(f"/api/runs/{run_id}/corrections", json={"fact_id": "F99", "text": "x"}, headers=headers())
    assert unknown.status_code == 404
    empty = client.post(f"/api/runs/{run_id}/corrections", json={"fact_id": "F1", "text": "  "}, headers=headers())
    assert empty.status_code == 422
    nothing = client.post(f"/api/runs/{run_id}/corrections", json={"fact_id": "F1"}, headers=headers())
    assert nothing.status_code == 422
    sample = client.get("/api/sample").json()
    bad_fault = client.post("/api/runs", json={"transcript": sample["transcript"], "faults": ["boom"]}, headers=headers())
    assert bad_fault.status_code == 422
    bad_rules = client.post("/api/runs", json={"transcript": sample["transcript"], "rules": {"roster": []}}, headers=headers())
    assert bad_rules.status_code == 422
    short = client.post("/api/runs", json={"transcript": "too short"}, headers=headers())
    assert short.status_code == 422


def test_only_real_changes_are_stored_as_corrections(client, start):
    run_id, _ = start()
    same = {"fact_id": "F2", "text": "Design handoff is due Friday 13 November", "owner": "Meera",
            "deadline_text": "Friday 13 November"}
    assert client.post(f"/api/runs/{run_id}/corrections", json=same, headers=headers()).status_code == 422
    edited = dict(same, deadline_text="Friday 20 November")
    assert client.post(f"/api/runs/{run_id}/corrections", json=edited, headers=headers()).status_code == 200
    d = fetch(client, run_id)
    assert d["context"]["corrections"] == {"F2": {"deadline_text": "Friday 20 November"}}
    back = client.post(f"/api/runs/{run_id}/corrections", json=same, headers=headers())
    assert back.status_code == 200
    assert fetch(client, run_id)["context"]["corrections"] == {}
