from datetime import date, datetime, timedelta

from .schemas import Issue


def parse_day(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def validate_rules(rules):
    if not isinstance(rules, dict):
        raise ValueError("rules must be a JSON object")
    roster = rules.get("roster")
    if not isinstance(roster, list) or not roster or not all(isinstance(p, dict) and p.get("name") for p in roster):
        raise ValueError("rules.roster must be a non-empty list of people with a name")
    params = rules.get("params")
    if not isinstance(params, dict) or parse_day(params.get("planning_start")) is None:
        raise ValueError("rules.params.planning_start must be a YYYY-MM-DD date")
    rule_list = rules.get("rules")
    if not isinstance(rule_list, list) or not all(isinstance(r, dict) and r.get("id") and r.get("text") for r in rule_list):
        raise ValueError("rules.rules must be a list of {id, text}")


def has_cycle(tasks):
    graph = {t.id: list(t.depends_on) for t in tasks}
    state = {}

    def visit(node):
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        for nxt in graph.get(node, []):
            if nxt in graph and visit(nxt):
                return True
        state[node] = 2
        return False

    return any(visit(n) for n in graph)


def ancestors(task_id, by_id, seen=None):
    seen = seen if seen is not None else set()
    task = by_id.get(task_id)
    if not task:
        return seen
    for dep in task.depends_on:
        if dep in by_id and dep not in seen:
            seen.add(dep)
            ancestors(dep, by_id, seen)
    return seen


def first_name(value):
    parts = (value or "").replace("(", " ").replace(")", " ").replace(",", " ").split()
    return parts[0].lower() if parts else ""


def check_plan(tasks, rules):
    params = rules.get("params", {})
    roster = {first_name(p["name"]): p["name"] for p in rules.get("roster", [])}
    max_tasks = params.get("max_tasks_per_owner")
    start = parse_day(params.get("planning_start"))
    latest = parse_day(params.get("latest_deadline"))
    launch_words = [w.lower() for w in params.get("launch_keywords", [])]
    gate_words = [w.lower() for w in params.get("gate_keywords", [])]
    by_id = {t.id: t for t in tasks}
    issues = []
    loads = {}

    for t in tasks:
        who = first_name(t.owner)
        if t.owner:
            if who not in roster:
                options = ", ".join(roster.values())
                issues.append(Issue(
                    task_id=t.id, rule_id="R1", source="rule-engine",
                    problem=f"{t.owner} is not on the roster",
                    correction=f"Give {t.id} to someone on the roster ({options}) or leave it unassigned",
                ))
            loads.setdefault(who, []).append(t.id)

        day = parse_day(t.deadline)
        if day:
            if day.weekday() >= 5:
                monday = day + timedelta(days=7 - day.weekday())
                friday = day - timedelta(days=day.weekday() - 4)
                issues.append(Issue(
                    task_id=t.id, rule_id="R3", source="rule-engine",
                    problem=f"{t.deadline} is a {day.strftime('%A')}",
                    correction=f"Move the deadline to a working day, for example {friday.isoformat()} or {monday.isoformat()}",
                ))
            if start and day < start:
                issues.append(Issue(
                    task_id=t.id, rule_id="R6", source="rule-engine",
                    problem=f"{t.deadline} is before the planning start {start.isoformat()}",
                    correction=f"Set a deadline on or after {start.isoformat()}",
                ))
            if latest and day > latest:
                issues.append(Issue(
                    task_id=t.id, rule_id="R6", source="rule-engine",
                    problem=f"{t.deadline} is after the latest allowed deadline {latest.isoformat()}",
                    correction=f"Pull the deadline in to {latest.isoformat()} or earlier",
                ))

    if max_tasks:
        for who, ids in loads.items():
            if len(ids) > max_tasks:
                issues.append(Issue(
                    task_id=ids[-1], rule_id="R4", source="rule-engine",
                    problem=f"{roster.get(who, who)} owns {len(ids)} tasks ({', '.join(ids)}), limit is {max_tasks}",
                    correction=f"Move at least {len(ids) - max_tasks} of these tasks to another person on the roster",
                ))

    if launch_words and gate_words:
        for t in tasks:
            title = t.title.lower()
            if not any(w in title for w in launch_words):
                continue
            if any(g in title for g in gate_words):
                continue
            before = ancestors(t.id, by_id)
            gated = any(any(g in by_id[a].title.lower() for g in gate_words) for a in before)
            if not gated:
                issues.append(Issue(
                    task_id=t.id, rule_id="R5", source="rule-engine",
                    problem=f"{t.id} is a launch task but does not depend on a security review",
                    correction=f"Add a security review task and make {t.id} depend on it",
                ))

    return issues


def inject_violation(plan):
    notes = []
    tasks = plan.get("tasks", [])
    if not tasks:
        return notes
    owned = next((t for t in tasks if t.get("owner")), tasks[0])
    owned["owner"] = "Rahul (contractor)"
    owned["owner_basis"] = "recommended"
    notes.append(f"SIMULATED FAULT: {owned['id']} was reassigned to 'Rahul (contractor)', who is not on the roster")
    dated = next((t for t in tasks if parse_day(t.get("deadline")) and t is not owned), None)
    if dated:
        day = parse_day(dated["deadline"])
        saturday = day + timedelta(days=5 - day.weekday()) if day.weekday() < 5 else day
        dated["deadline"] = saturday.isoformat()
        dated["deadline_basis"] = "recommended"
        notes.append(f"SIMULATED FAULT: {dated['id']} deadline moved to Saturday {saturday.isoformat()}")
    return notes
