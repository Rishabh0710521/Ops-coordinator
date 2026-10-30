import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, orchestrator, rules, sample

STATIC = Path(__file__).parent / "static"
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_RUNS_PER_SESSION = 25
MAX_RUNNING_PER_SESSION = 2


@asynccontextmanager
async def lifespan(_app):
    db.init()
    yield


app = FastAPI(title="Ops Coordinator", lifespan=lifespan)


class RunRequest(BaseModel):
    transcript: str = Field(min_length=40, max_length=12000)
    rules: Optional[dict] = None
    faults: list[str] = Field(default_factory=list)
    title: Optional[str] = Field(default=None, max_length=80)


class CorrectionRequest(BaseModel):
    fact_id: str
    text: Optional[str] = Field(default=None, max_length=400)
    owner: Optional[str] = Field(default=None, max_length=80)
    deadline_text: Optional[str] = Field(default=None, max_length=120)
    revert: bool = False


class ResolutionRequest(BaseModel):
    flag_id: str
    answer: str = Field(min_length=1, max_length=500)


def session_id(x_session_id: str = Header(default="")):
    if not SESSION_RE.match(x_session_id):
        raise HTTPException(400, "Missing or invalid X-Session-Id header")
    return x_session_id


def owned_run(run_id, sid):
    run = db.fetch_run(run_id, sid)
    if not run:
        raise HTTPException(404, "Run not found in this session")
    return run


def busy(run):
    if run["status"] == "running":
        raise HTTPException(409, "This run is still working. Wait for it to finish before changing it.")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def get_config():
    return {
        "llm_ready": bool(config.LLM_API_KEY),
        "model": config.LLM_MODEL,
        "max_review_rounds": config.MAX_REVIEW_ROUNDS,
        "fault_kinds": list(orchestrator.FAULT_KINDS),
    }


@app.get("/api/sample")
def get_sample():
    return {"transcript": sample.TRANSCRIPT, "rules": sample.RULES}


@app.get("/api/runs")
def runs(sid: str = Depends(session_id)):
    return orchestrator.list_runs(sid)


@app.post("/api/runs", status_code=201)
def create(body: RunRequest, sid: str = Depends(session_id)):
    bad = [f for f in body.faults if f not in orchestrator.FAULT_KINDS]
    if bad:
        raise HTTPException(422, "Unknown fault: " + ", ".join(bad))
    chosen = body.rules if body.rules is not None else sample.RULES
    try:
        rules.validate_rules(chosen)
    except ValueError as err:
        raise HTTPException(422, str(err))
    if len(orchestrator.list_runs(sid)) >= MAX_RUNS_PER_SESSION:
        raise HTTPException(429, "Run limit reached for this session. Use Reset to clear old runs.")
    if orchestrator.count_running(sid) >= MAX_RUNNING_PER_SESSION:
        raise HTTPException(429, "Two runs are already in progress in this session.")
    title = (body.title or "").strip() or body.transcript.strip().splitlines()[0][:60]
    run_id = orchestrator.create_run(sid, body.transcript, chosen, body.faults, title)
    orchestrator.launch(run_id)
    return {"id": run_id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, sid: str = Depends(session_id)):
    owned_run(run_id, sid)
    return orchestrator.detail(run_id)


@app.post("/api/runs/{run_id}/resume")
def resume(run_id: str, sid: str = Depends(session_id)):
    run = owned_run(run_id, sid)
    if run["status"] != "failed":
        raise HTTPException(409, "Only a failed run can be resumed")
    if not orchestrator.launch(run_id, ("failed",)):
        raise HTTPException(409, "Run is already being resumed")
    return {"ok": True}


@app.post("/api/runs/{run_id}/corrections")
def correct(run_id: str, body: CorrectionRequest, sid: str = Depends(session_id)):
    run = owned_run(run_id, sid)
    busy(run)
    intake = orchestrator.intake_output(run_id)
    if not intake:
        raise HTTPException(409, "The Intake Agent has not produced facts for this run yet")
    if body.fact_id not in {f["id"] for f in intake["facts"]}:
        raise HTTPException(404, f"No fact {body.fact_id} in this run")

    ctx = orchestrator.latest_context(run_id)
    corrections = dict(ctx["corrections"])

    if body.revert:
        if body.fact_id not in corrections:
            raise HTTPException(409, f"{body.fact_id} has no correction to undo")
        corrections.pop(body.fact_id)
        summary = f"Correction on {body.fact_id} undone"
    else:
        original = next(f for f in intake["facts"] if f["id"] == body.fact_id)
        patch = dict(corrections.get(body.fact_id, {}))
        sent = body.model_fields_set
        if "text" in sent and body.text is not None:
            if not body.text.strip():
                raise HTTPException(422, "Fact text cannot be empty")
            patch["text"] = body.text.strip()
        for field in ("owner", "deadline_text"):
            if field in sent:
                value = getattr(body, field)
                patch[field] = value.strip() if value and value.strip() else None
        patch = {k: v for k, v in patch.items() if v != original.get(k)}
        if not patch:
            if body.fact_id not in corrections:
                raise HTTPException(422, "Nothing to change")
            corrections.pop(body.fact_id)
            summary = f"Correction on {body.fact_id} undone"
        else:
            corrections[body.fact_id] = patch
            summary = f"User corrected {body.fact_id} ({', '.join(patch)})"

    version = orchestrator.revise(run_id, "correction", summary, corrections=corrections)
    if version is None:
        raise HTTPException(409, "This run is still working. Wait for it to finish before changing it.")
    orchestrator.launch(run_id, ("queued",))
    return {"context_version": version}


@app.post("/api/runs/{run_id}/resolutions")
def resolve(run_id: str, body: ResolutionRequest, sid: str = Depends(session_id)):
    run = owned_run(run_id, sid)
    busy(run)
    intake = orchestrator.intake_output(run_id)
    if not intake:
        raise HTTPException(409, "The Intake Agent has not produced flags for this run yet")
    if body.flag_id not in {f["id"] for f in intake["flags"]}:
        raise HTTPException(404, f"No flag {body.flag_id} in this run")
    ctx = orchestrator.latest_context(run_id)
    resolutions = dict(ctx["resolutions"])
    resolutions[body.flag_id] = body.answer.strip()
    version = orchestrator.revise(
        run_id, "resolution", f"User answered open item {body.flag_id}", resolutions=resolutions
    )
    if version is None:
        raise HTTPException(409, "This run is still working. Wait for it to finish before changing it.")
    orchestrator.launch(run_id, ("queued",))
    return {"context_version": version}


@app.post("/api/reset")
def reset(sid: str = Depends(session_id)):
    removed, skipped = orchestrator.reset_session(sid)
    return {"removed": removed, "skipped_running": skipped}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
