import pytest

from app import rules, sample
from app.schemas import Task

RULES = sample.RULES


def task(tid, **kw):
    base = {"id": tid, "title": f"Task {tid}"}
    base.update(kw)
    return Task(**base)


def hits(tasks):
    return {(i.rule_id, i.task_id) for i in rules.check_plan(tasks, RULES)}


def test_clean_plan_has_no_findings():
    plan = [
        task("T1", owner="Arjun", deadline="2026-11-13"),
        task("T2", title="Security review", owner="Sana", deadline="2026-12-02", depends_on=["T1"]),
        task("T3", title="Launch pilot", owner="Priya", deadline="2026-12-04", depends_on=["T2"]),
    ]
    assert hits(plan) == set()


def test_unknown_owner_is_flagged():
    assert ("R1", "T1") in hits([task("T1", owner="Rahul (contractor)")])


def test_roster_match_uses_first_name():
    assert hits([task("T1", owner="Arjun (Backend Engineer)")]) == set()


def test_weekend_deadline_is_flagged_with_a_suggestion():
    found = rules.check_plan([task("T1", owner="Arjun", deadline="2026-11-14")], RULES)
    assert found[0].rule_id == "R3"
    assert "2026-11-13" in found[0].correction and "2026-11-16" in found[0].correction


def test_deadline_window():
    found = hits([task("T1", deadline="2026-10-30"), task("T2", deadline="2026-12-14")])
    assert ("R6", "T1") in found and ("R6", "T2") in found


def test_owner_load_limit():
    plan = [task(f"T{i}", owner="Arjun") for i in range(1, 5)]
    assert ("R4", "T4") in hits(plan)


def test_launch_without_security_review_is_flagged():
    plan = [task("T1", title="Launch pilot", owner="Priya", deadline="2026-12-04")]
    assert ("R5", "T1") in hits(plan)


def test_launch_depending_on_review_through_a_chain_passes():
    plan = [
        task("T1", title="Security review", owner="Sana"),
        task("T2", title="Fix findings", owner="Arjun", depends_on=["T1"]),
        task("T3", title="Go live", owner="Priya", depends_on=["T2"]),
    ]
    assert not any(rule == "R5" for rule, _ in hits(plan))


def test_cycle_detection():
    assert rules.has_cycle([task("T1", depends_on=["T2"]), task("T2", depends_on=["T1"])])
    assert not rules.has_cycle([task("T1"), task("T2", depends_on=["T1"])])


def test_inject_violation_marks_itself():
    plan = {"tasks": [
        {"id": "T1", "owner": "Arjun", "deadline": "2026-11-13"},
        {"id": "T2", "owner": "Meera", "deadline": "2026-11-12"},
    ]}
    notes = rules.inject_violation(plan)
    assert all(n.startswith("SIMULATED FAULT") for n in notes)
    assert plan["tasks"][0]["owner"] == "Rahul (contractor)"
    assert plan["tasks"][1]["deadline"] == "2026-11-14"


@pytest.mark.parametrize("bad", [
    {},
    {"roster": [], "params": {}, "rules": []},
    {"roster": [{"name": "A"}], "params": {"planning_start": "soon"}, "rules": []},
    {"roster": [{"name": "A"}], "params": {"planning_start": "2026-11-02"}, "rules": [{"id": "R1"}]},
])
def test_rules_validation_rejects_bad_shapes(bad):
    with pytest.raises(ValueError):
        rules.validate_rules(bad)


def test_sample_rules_are_valid():
    rules.validate_rules(RULES)
