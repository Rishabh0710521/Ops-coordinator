import hashlib
import json
import threading
import uuid

from pydantic import ValidationError

from . import agents, config, db
from . import rules as rule_engine
from .schemas import FeedbackMessage, PlanningInput, ReviewInput

SYNC = False
AGENT_NAMES = ("intake", "planning", "review")
FAULT_KINDS = ("fail:intake", "fail:planning", "fail:review", "violation:planning")


class SimulatedFault(Exception):
    pass


class HandoffError(Exception):
    pass


class StepFailure(Exception):
    def __init__(self, agent, message, simulated=False):
        super().__init__(message)
        self.agent = agent
        self.simulated = simulated


def digest(obj):
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def log(run_id, level, message):
    with db.tx() as c:
        c.execute(
            "INSERT INTO events (run_id, level, message, created_at) VALUES (?,?,?,?)",
            (run_id, level, message, db.now()),
        )


def read_context(row):
    return {
        "version": row["version"],
        "kind": row["kind"],
        "summary": row["summary"],
        "rules": json.loads(row["rules"]),
        "corrections": json.loads(row["corrections"]),
        "resolutions": json.loads(row["resolutions"]),
        "created_at": row["created_at"],
    }


def latest_context(run_id, conn=None):
    sql = "SELECT * FROM contexts WHERE run_id=? ORDER BY version DESC LIMIT 1"
    if conn is not None:
        return read_context(conn.execute(sql, (run_id,)).fetchone())
    with db.tx() as c:
        return read_context(c.execute(sql, (run_id,)).fetchone())


def create_run(session_id, transcript, rules, faults, title):
    run_id = uuid.uuid4().hex[:12]
    stamp = db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO runs (id, session_id, title, transcript, status, faults, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, session_id, title, transcript, "queued", json.dumps(faults), stamp, stamp),
        )
        c.execute(
            "INSERT INTO contexts (run_id, version, kind, summary, rules, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, 1, "initial", "Transcript and company rules loaded", json.dumps(rules), stamp),
        )
    log(run_id, "info", "Run created with context v1")
    if faults:
        log(run_id, "warn", "Simulated faults armed for this run: " + ", ".join(faults))
    return run_id


def claim(run_id, allowed):
    marks = ",".join("?" for _ in allowed)
    with db.tx() as c:
        cur = c.execute(
            f"UPDATE runs SET status='running', failed_step=NULL, note=NULL, updated_at=? "
            f"WHERE id=? AND status IN ({marks})",
            (db.now(), run_id, *allowed),
        )
        return cur.rowcount == 1


def revise(run_id, kind, summary, corrections=None, resolutions=None):
    with db.tx() as c:
        cur = c.execute(
            "UPDATE runs SET status='queued', prev_result=COALESCE(result, prev_result), result=NULL, "
            "failed_step=NULL, note=NULL, updated_at=? WHERE id=? AND status!='running'",
            (db.now(), run_id),
        )
        if cur.rowcount != 1:
            return None
        ctx = latest_context(run_id, c)
        version = ctx["version"] + 1
        c.execute(
            "INSERT INTO contexts (run_id, version, kind, summary, rules, corrections, resolutions, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                version,
                kind,
                summary,
                json.dumps(ctx["rules"]),
                json.dumps(corrections if corrections is not None else ctx["corrections"]),
                json.dumps(resolutions if resolutions is not None else ctx["resolutions"]),
                db.now(),
            ),
        )
    log(run_id, "info", f"Context v{version} created: {summary}")
    return version


def intake_output(run_id):
    with db.tx() as c:
        row = c.execute(
            "SELECT output FROM steps WHERE run_id=? AND agent='intake' AND status='done' ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    return json.loads(row["output"]) if row else None


def take_fault(run_id, wanted):
    with db.tx() as c:
        row = c.execute("SELECT faults FROM runs WHERE id=?", (run_id,)).fetchone()
        faults = json.loads(row["faults"])
        hit = [f for f in faults if f in wanted]
        if not hit:
            return None
        faults.remove(hit[0])
        c.execute("UPDATE runs SET faults=? WHERE id=?", (json.dumps(faults), run_id))
        return hit[0]


def run_step(run_id, agent, attempt, version, payload, worker):
    key = digest(payload)
    reused = None
    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM steps WHERE run_id=? AND agent=? AND attempt=? AND input_hash=?",
            (run_id, agent, attempt, key),
        ).fetchone()
        if row and row["status"] == "done":
            reused = json.loads(row["output"])
        elif row:
            step_id = row["id"]
            c.execute(
                "UPDATE steps SET status='running', error=NULL, retries=retries+1, context_version=?, "
                "started_at=?, finished_at=NULL WHERE id=?",
                (version, db.now(), step_id),
            )
        else:
            cur = c.execute(
                "INSERT INTO steps (run_id, agent, attempt, input_hash, context_version, status, input, started_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, agent, attempt, key, version, "running", json.dumps(payload), db.now()),
            )
            step_id = cur.lastrowid

    if reused is not None:
        log(run_id, "info", f"{agent.title()} Agent attempt {attempt}: identical input already processed, saved output reused (no model call)")
        return reused

    log(run_id, "info", f"{agent.title()} Agent attempt {attempt} started on context v{version}")
    fault = take_fault(run_id, (f"fail:{agent}", f"violation:{agent}"))
    simulated = 0
    try:
        if fault and fault.startswith("fail:"):
            raise SimulatedFault("SIMULATED model failure injected for this demo: the model call was never made")
        output, usage, notes = worker(payload)
        if fault and fault.startswith("violation:"):
            notes = notes + rule_engine.inject_violation(output)
            simulated = 1
    except Exception as err:
        with db.tx() as c:
            c.execute(
                "UPDATE steps SET status='failed', error=?, simulated=?, finished_at=? WHERE id=?",
                (str(err), int(isinstance(err, SimulatedFault)), db.now(), step_id),
            )
        raise StepFailure(agent, str(err), isinstance(err, SimulatedFault))

    with db.tx() as c:
        c.execute(
            "UPDATE steps SET status='done', output=?, notes=?, simulated=?, tokens_in=?, tokens_out=?, finished_at=? "
            "WHERE id=?",
            (json.dumps(output), json.dumps(notes), simulated, usage["in"], usage["out"], db.now(), step_id),
        )
    for note in notes:
        log(run_id, "warn" if "SIMULATED" in note else "info", f"{agent.title()} Agent: {note}")
    log(run_id, "info", f"{agent.title()} Agent attempt {attempt} finished")
    return output


def handoff(run_id, sender, receiver, kind, payload, model, version):
    issues = []
    try:
        model.model_validate(payload)
    except ValidationError as err:
        issues = [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in err.errors()][:8]
    with db.tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO handoffs "
            "(run_id, from_agent, to_agent, kind, payload, payload_hash, valid, issues, context_version, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, sender, receiver, kind, json.dumps(payload), digest(payload), int(not issues),
             json.dumps(issues), version, db.now()),
        )
    if issues:
        raise HandoffError(f"{sender} to {receiver} handoff ({kind}) failed schema validation: " + "; ".join(issues))


def apply_corrections(facts, corrections):
    out = []
    for fact in facts:
        patch = corrections.get(fact["id"])
        if patch:
            fact = dict(fact)
            for key, value in patch.items():
                if value is None:
                    fact.pop(key, None)
                else:
                    fact[key] = value
            fact["corrected"] = True
        out.append(fact)
    return out


def pipeline(run_id):
    run = db.fetch_run(run_id)
    ctx = latest_context(run_id)
    version = ctx["version"]
    rules = ctx["rules"]
    lines = agents.number_lines(run["transcript"])

    intake = run_step(run_id, "intake", 1, version, {"transcript": lines},
                      lambda p: agents.run_intake(p["transcript"]))

    facts = apply_corrections(intake["facts"], ctx["corrections"])
    answered = ctx["resolutions"]
    flags = intake["flags"]
    resolutions = [
        {"flag_id": f["id"], "flag_text": f["text"], "answer": answered[f["id"]]}
        for f in flags if f["id"] in answered
    ]
    open_flags = [f for f in flags if f["id"] not in answered]

    planning_in = {
        "planning_start": rules["params"]["planning_start"],
        "facts": facts,
        "open_flags": open_flags,
        "resolutions": resolutions,
        "rules": rules,
        "previous_plan": None,
        "review_feedback": [],
    }

    plan = None
    review = None
    rounds = 0
    for rnd in range(1, config.MAX_REVIEW_ROUNDS + 1):
        rounds = rnd
        if rnd == 1:
            handoff(run_id, "intake", "planning", "facts", planning_in, PlanningInput, version)
        plan = run_step(run_id, "planning", rnd, version, planning_in, agents.run_planning)

        review_in = {"transcript": lines, "facts": facts, "resolutions": resolutions, "rules": rules, "plan": plan}
        handoff(run_id, "planning", "review", "plan", review_in, ReviewInput, version)
        review = run_step(run_id, "review", rnd, version, review_in, agents.run_review)

        if review["verdict"] == "pass":
            break
        if rnd < config.MAX_REVIEW_ROUNDS:
            handoff(run_id, "review", "planning", "feedback",
                    {"round": rnd, "issues": review["issues"]}, FeedbackMessage, version)
            planning_in = dict(planning_in, previous_plan=plan, review_feedback=review["issues"])

    blocking = [i for i in review["issues"] if i["severity"] == "blocking"]
    passed = review["verdict"] == "pass"
    result = {
        "context_version": version,
        "facts": facts,
        "flags": flags,
        "open_flags": open_flags,
        "resolutions": resolutions,
        "plan": plan,
        "review": review,
        "rounds": rounds,
        "unresolved_issues": [] if passed else blocking,
    }
    note = None
    if not passed:
        note = f"Stopped after {rounds} review rounds with {len(blocking)} blocking issue(s) still open"
    save_result(run_id, version, "complete" if passed else "complete_with_issues", result, note)


def save_result(run_id, version, status, result, note):
    with db.tx() as c:
        current = latest_context(run_id, c)["version"]
        if current != version:
            discarded = True
        else:
            discarded = False
            c.execute(
                "UPDATE runs SET status=?, result=?, note=?, failed_step=NULL, updated_at=? WHERE id=?",
                (status, json.dumps(result), note, db.now(), run_id),
            )
    if discarded:
        log(run_id, "warn", f"Result for context v{version} discarded because v{current} exists")
    else:
        log(run_id, "info", f"Run finished: {status.replace('_', ' ')}")


def mark_failed(run_id, step, message):
    with db.tx() as c:
        c.execute(
            "UPDATE runs SET status='failed', failed_step=?, note=?, updated_at=? WHERE id=?",
            (step, message, db.now(), run_id),
        )
    log(run_id, "error", f"Run stopped at {step}: {message}")


def execute(run_id):
    try:
        pipeline(run_id)
    except StepFailure as err:
        mark_failed(run_id, err.agent, str(err))
    except HandoffError as err:
        mark_failed(run_id, "handoff", str(err))
    except Exception as err:
        mark_failed(run_id, "orchestrator", f"unexpected {type(err).__name__}: {err}")


def launch(run_id, allowed=("queued", "failed")):
    if not claim(run_id, allowed):
        return False
    if SYNC:
        execute(run_id)
    else:
        threading.Thread(target=execute, args=(run_id,), daemon=True).start()
    return True


def reset_session(session_id):
    removed = 0
    skipped = 0
    with db.tx() as c:
        rows = c.execute("SELECT id, status FROM runs WHERE session_id=?", (session_id,)).fetchall()
        for row in rows:
            if row["status"] == "running":
                skipped += 1
                continue
            for table in ("steps", "handoffs", "events", "contexts"):
                c.execute(f"DELETE FROM {table} WHERE run_id=?", (row["id"],))
            c.execute("DELETE FROM runs WHERE id=?", (row["id"],))
            removed += 1
    return removed, skipped


def list_runs(session_id):
    with db.tx() as c:
        rows = c.execute(
            "SELECT id, title, status, created_at FROM runs WHERE session_id=? ORDER BY created_at DESC, rowid DESC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_running(session_id):
    with db.tx() as c:
        return c.execute(
            "SELECT COUNT(*) FROM runs WHERE session_id=? AND status='running'", (session_id,)
        ).fetchone()[0]


def agent_summary(steps):
    out = {}
    for name in AGENT_NAMES:
        mine = [s for s in steps if s["agent"] == name]
        if not mine:
            out[name] = {"status": "idle", "attempts": 0, "tokens_in": 0, "tokens_out": 0}
            continue
        last = mine[-1]
        out[name] = {
            "status": last["status"],
            "attempts": len({s["attempt"] for s in mine}),
            "tokens_in": sum(s["tokens_in"] for s in mine),
            "tokens_out": sum(s["tokens_out"] for s in mine),
        }
    return out


def propagation(ctx, intake, result, prev):
    original = {f["id"]: f for f in (intake or {}).get("facts", [])}
    corrections = []
    for fact_id, patch in ctx["corrections"].items():
        before = original.get(fact_id, {})
        corrections.append({
            "fact_id": fact_id,
            "text": before.get("text"),
            "fields": [
                {"field": k, "before": before.get(k), "after": v} for k, v in patch.items()
            ],
        })
    affected = {}
    changes = []
    if result:
        for cor in corrections:
            affected[cor["fact_id"]] = [
                t["id"] for t in result["plan"]["tasks"] if cor["fact_id"] in t.get("supported_by", [])
            ]
    if result and prev:
        old = {t["id"]: t for t in prev["plan"]["tasks"]}
        for task in result["plan"]["tasks"]:
            before = old.get(task["id"])
            if not before:
                changes.append({"task_id": task["id"], "field": "task", "before": None, "after": task["title"]})
                continue
            for field in ("owner", "deadline", "title"):
                if before.get(field) != task.get(field):
                    changes.append({"task_id": task["id"], "field": field,
                                    "before": before.get(field), "after": task.get(field)})
        for tid, before in old.items():
            if tid not in {t["id"] for t in result["plan"]["tasks"]}:
                changes.append({"task_id": tid, "field": "task", "before": before["title"], "after": None})
    return {"corrections": corrections, "affected_tasks": affected, "changes": changes}


def detail(run_id):
    run = db.fetch_run(run_id)
    with db.tx() as c:
        ctx_rows = c.execute("SELECT * FROM contexts WHERE run_id=? ORDER BY version", (run_id,)).fetchall()
        step_rows = c.execute("SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        hand_rows = c.execute("SELECT * FROM handoffs WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        event_rows = c.execute(
            "SELECT level, message, created_at FROM events WHERE run_id=? ORDER BY id DESC LIMIT 120", (run_id,)
        ).fetchall()

    contexts = [read_context(r) for r in ctx_rows]
    steps = []
    for r in step_rows:
        steps.append({
            "id": r["id"],
            "agent": r["agent"],
            "attempt": r["attempt"],
            "context_version": r["context_version"],
            "status": r["status"],
            "input": json.loads(r["input"]),
            "output": db.loads(r["output"]),
            "notes": json.loads(r["notes"]),
            "error": r["error"],
            "simulated": bool(r["simulated"]),
            "retries": r["retries"],
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
        })
    handoffs = [{
        "id": r["id"],
        "from": r["from_agent"],
        "to": r["to_agent"],
        "kind": r["kind"],
        "payload": json.loads(r["payload"]),
        "valid": bool(r["valid"]),
        "issues": json.loads(r["issues"]),
        "context_version": r["context_version"],
        "created_at": r["created_at"],
    } for r in hand_rows]

    result = db.loads(run["result"])
    prev = db.loads(run["prev_result"])
    tokens_in = sum(s["tokens_in"] for s in steps)
    tokens_out = sum(s["tokens_out"] for s in steps)
    intake = next((s["output"] for s in reversed(steps) if s["agent"] == "intake" and s["status"] == "done"), None)

    return {
        "run": {
            "id": run["id"],
            "title": run["title"],
            "status": run["status"],
            "failed_step": run["failed_step"],
            "note": run["note"],
            "faults": json.loads(run["faults"]),
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "max_rounds": config.MAX_REVIEW_ROUNDS,
        },
        "lines": agents.number_lines(run["transcript"]),
        "context": contexts[-1],
        "contexts": [{k: v for k, v in c.items() if k != "rules"} for c in contexts],
        "agents": agent_summary(steps),
        "steps": steps,
        "handoffs": handoffs,
        "events": list(reversed([dict(r) for r in event_rows])),
        "result": result,
        "propagation": propagation(contexts[-1], intake, result, prev),
        "cost": {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "usd": round(tokens_in / 1e6 * config.PRICE_IN + tokens_out / 1e6 * config.PRICE_OUT, 5),
        },
    }
