from pydantic import ValidationError

from . import llm, prompts
from . import rules as rule_engine
from .schemas import IntakeOutput, PlanOutput, ReviewOutput


class AgentError(Exception):
    pass


def number_lines(transcript):
    rows = [ln.strip() for ln in transcript.splitlines() if ln.strip()]
    return [{"label": f"L{i}", "text": text} for i, text in enumerate(rows, 1)]


def strip_nulls(value):
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value if v is not None]
    return value


def ask(system, payload, model, check):
    usage = {"in": 0, "out": 0}
    problems = []
    for _ in range(2):
        body = dict(payload)
        if problems:
            body["fix_these_problems"] = problems
        raw, used = llm.complete_json(system, body)
        usage["in"] += used["in"]
        usage["out"] += used["out"]
        try:
            obj = model.model_validate(strip_nulls(raw))
        except ValidationError as err:
            problems = [
                ".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in err.errors()
            ][:8]
            continue
        problems = check(obj)
        if not problems:
            return obj, usage
    raise AgentError("output rejected after one repair attempt: " + "; ".join(problems))


def check_intake(out):
    problems = []
    if not out.facts:
        problems.append("facts is empty")
    ids = [f.id for f in out.facts] + [f.id for f in out.flags]
    if len(set(ids)) != len(ids):
        problems.append("fact and flag ids must be unique")
    return problems


def ground_intake(out, lines):
    text_by = {l["label"]: l["text"].lower() for l in lines}
    notes = []
    kept = []
    for fact in out.facts:
        fact.source_refs = [r for r in fact.source_refs if r in text_by]
        if not fact.source_refs:
            notes.append(f"dropped {fact.id}: it cites no line that exists in the transcript")
            continue
        cited = " ".join(text_by[r] for r in fact.source_refs)
        if fact.owner and rule_engine.first_name(fact.owner) not in cited:
            notes.append(f"removed owner '{fact.owner}' from {fact.id}: not named in {', '.join(fact.source_refs)}")
            fact.owner = None
        if fact.deadline_text and fact.deadline_text.lower() not in cited:
            notes.append(f"removed deadline '{fact.deadline_text}' from {fact.id}: not in {', '.join(fact.source_refs)}")
            fact.deadline_text = None
        kept.append(fact)
    out.facts = kept
    known = {f.id for f in kept}
    for flag in out.flags:
        flag.source_refs = [r for r in flag.source_refs if r in text_by]
        flag.related_fact_ids = [i for i in flag.related_fact_ids if i in known]
    out.flags = [f for f in out.flags if f.source_refs or f.kind == "missing"]
    return notes


def run_intake(lines):
    out, usage = ask(prompts.INTAKE_SYSTEM, {"transcript": lines}, IntakeOutput, check_intake)
    notes = ground_intake(out, lines)
    return out.model_dump(), usage, notes


def check_plan_shape(plan):
    problems = []
    ids = [t.id for t in plan.tasks]
    if not ids:
        problems.append("plan has no tasks")
    if len(set(ids)) != len(ids):
        problems.append("task ids must be unique")
    known = set(ids)
    for t in plan.tasks:
        for dep in t.depends_on:
            if dep == t.id:
                problems.append(f"{t.id} depends on itself")
            elif dep not in known:
                problems.append(f"{t.id} depends on unknown task {dep}")
        if t.deadline and rule_engine.parse_day(t.deadline) is None:
            problems.append(f"{t.id} deadline must be YYYY-MM-DD")
    if not problems and rule_engine.has_cycle(plan.tasks):
        problems.append("dependencies form a cycle")
    return problems


def ground_plan(plan, facts):
    by_id = {f["id"]: f for f in facts}
    notes = []
    for t in plan.tasks:
        t.supported_by = [i for i in t.supported_by if i in by_id]
        if t.basis == "supported" and not t.supported_by:
            t.basis = "recommendation"
            notes.append(f"{t.id} was marked supported but cites no known fact, downgraded to recommendation")
        if t.owner and t.owner_basis == "stated":
            named = {rule_engine.first_name(by_id[i].get("owner")) for i in t.supported_by}
            if rule_engine.first_name(t.owner) not in named:
                t.owner_basis = "recommended"
                notes.append(f"{t.id} owner '{t.owner}' is not stated in its supporting facts, relabelled as recommended")
        if t.deadline and t.deadline_basis == "stated":
            if not any(by_id[i].get("deadline_text") for i in t.supported_by):
                t.deadline_basis = "recommended"
                notes.append(f"{t.id} deadline is not stated in its supporting facts, relabelled as recommended")
        if not t.owner:
            t.owner_basis = "unassigned"
        if not t.deadline:
            t.deadline_basis = "none"
    return notes


def run_planning(planning_in):
    facts = planning_in["facts"]
    out, usage = ask(prompts.PLANNING_SYSTEM, planning_in, PlanOutput, check_plan_shape)
    notes = ground_plan(out, facts)
    return out.model_dump(), usage, notes


def run_review(review_in):
    plan = PlanOutput.model_validate(review_in["plan"])
    known_tasks = {t.id for t in plan.tasks}

    def check(out):
        if out.verdict == "fail" and not out.issues:
            return ["verdict is fail but no issues were listed"]
        return []

    out, usage = ask(prompts.REVIEW_SYSTEM, review_in, ReviewOutput, check)
    notes = []
    for issue in out.issues:
        issue.source = "review-agent"
        if issue.task_id and issue.task_id not in known_tasks:
            issue.task_id = None

    seen = {(i.task_id, i.rule_id) for i in out.issues if i.rule_id}
    for found in rule_engine.check_plan(plan.tasks, review_in["rules"]):
        if (found.task_id, found.rule_id) not in seen:
            out.issues.append(found)
            notes.append(f"rule engine added {found.rule_id} finding on {found.task_id}")

    for n, issue in enumerate(out.issues, 1):
        issue.id = f"I{n}"
    blocking = any(i.severity == "blocking" for i in out.issues)
    out.verdict = "fail" if blocking else "pass"
    return out.model_dump(), usage, notes
