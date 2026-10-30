# Ops Coordinator

Assignment 02 for the Mudita AI Generalist brief. Three separate LLM agents turn a fictional meeting transcript and a set of company rules into a reviewed action plan. The agents never share a chat history. They only exchange validated, stored messages.

Live URL: add after deploying  
Demo access: no login. Open the URL, click "Run the three agents". Each browser gets its own session automatically.

## What it does

1. **Intake Agent** reads the numbered transcript and returns facts (decisions, requirements, constraints) with line references, plus flags for anything missing or conflicting. It is told not to invent owners or deadlines, and the code double-checks that: an owner whose name does not appear in the cited lines, or a deadline whose words do not appear there, is stripped and noted.
2. **Planning Agent** never sees the transcript. It gets the facts, open flags, user answers and the rules, and proposes tasks. Every task says whether its owner and deadline are *stated* or *recommended*, and whether the task itself is *supported* by facts or is a *recommendation*. Open conflicts become unresolved questions instead of being quietly settled.
3. **Review Agent** gets the original transcript, the facts, the rules and the plan, and returns specific corrections. A small deterministic rule engine runs on the same plan, so a clear violation (unknown owner, weekend deadline, too many tasks, launch without a security review, deadline outside the window) is always caught, whatever the model says.
4. Corrections go back to the Planning Agent, the revision is rechecked, and the loop stops after `MAX_REVIEW_ROUNDS` (default 3). If blocking issues remain, the run ends as "stopped with open issues" and lists them.

## Run it locally

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open http://localhost:8000. Put a real key in `.env` first. Any OpenAI-compatible chat endpoint that supports JSON mode works; the defaults point at Groq.

Tests need no key and no network:

```
python -m pytest
```

Docker:

```
docker build -t ops-coordinator .
docker run -p 8000:8000 -e LLM_API_KEY=... -v ops-data:/srv/data ops-coordinator
```

## Environment variables

| Name | Purpose | Default |
| --- | --- | --- |
| `LLM_API_KEY` | Model provider key, server side only | none |
| `LLM_BASE_URL` | OpenAI-compatible base URL | `https://api.groq.com/openai/v1` |
| `LLM_MODEL` | Model name | `llama-3.3-70b-versatile` |
| `LLM_TIMEOUT` | Seconds per model call | `60` |
| `MAX_REVIEW_ROUNDS` | Planning/review attempts before stopping | `3` |
| `PRICE_PER_M_INPUT` | Used for the on-screen cost estimate | `0.59` |
| `PRICE_PER_M_OUTPUT` | Used for the on-screen cost estimate | `0.79` |
| `DB_PATH` | SQLite file | `data/coordinator.db` |

The key is only read by the server. It is never sent to the browser or committed (`.env` is git-ignored).

## Architecture

```
app/
  main.py          FastAPI routes, session check, input limits
  orchestrator.py  run lifecycle, step runner, handoffs, resume, context versions
  agents.py        the three agent functions, output repair, grounding checks
  prompts.py       one system prompt per agent
  schemas.py       pydantic models for every agent output and every handoff
  rules.py         deterministic rule checks and the simulated rule violation
  llm.py           HTTP client with retry on 429/5xx and JSON enforcement
  db.py            SQLite schema
  static/          vanilla JS interface
```

Tables: `runs`, `contexts`, `steps`, `handoffs`, `events`.

### How context is shared

- Every run has an ID. Each user's browser holds a random session ID that is sent as `X-Session-Id`. Every query filters on it, and another session asking for the run gets a 404. Agents are stateless HTTP calls that only receive the data of one run, so nothing can leak between runs or sessions.
- **Context versions.** Version 1 holds the transcript and rules. Each user correction or answer to a flag creates a new version with the corrections layered over the original extracted facts. Nothing is edited in place.
- **Handoffs.** Intake to Planning, Planning to Review and Review to Planning are each stored as a message and validated against a schema before the receiving agent runs. A message that fails validation stops the run and is shown as rejected.
- **No repeated work.** A step is keyed by run, agent, attempt and a hash of its exact input. If the same key already finished, its saved output is reused and no model call is made. This is what makes resume and partial reruns safe.
- **No stale context.** Changing a fact changes the Planning input, so Planning and Review get new keys and run again, while Intake is reused. A result is only saved if its context version is still the latest one. Corrections are refused while a run is working.
- **Resume.** A failed step is marked failed with its error. Resume re-enters the pipeline; finished steps are reused and only the failed one runs again. Runs left "running" by a server restart are marked failed on startup so they can be resumed.

### Simulated faults

The "Simulated faults" box on the form arms one-shot faults for a run. They are labelled "simulated" in the status note, the step cards, the activity log and the rule-violation notes.

- Model failure in any agent: the step fails before any model call. Resume shows recovery.
- Rule violation: after the Planning Agent returns its first plan, the app reassigns one task to someone not on the roster and moves another to a Saturday. The review loop has to catch and fix it.

## Where each required case is shown

| Case | How to see it |
| --- | --- |
| One complete three-agent run | Run the sample with no faults |
| Rule violation sent back for correction | Tick "Slip a rule violation into the first plan"; see the review-to-planning feedback handoff and two attempts each |
| Changed source fact propagating | Click Correct on F2, change the deadline, save; the effect panel shows before and after and which tasks were built on that fact |
| Recovery after a simulated model failure | Tick "Model failure in the Planning Agent", then Resume; Intake is not run again |
| Two sessions not sharing context | "Open this run from another session" button, or New session in the header |
| Sample input and reset | Load sample, Reset my runs |

## Tests

37 tests. They use a scripted stand-in for the model, so they check the orchestration, not model quality: full run, labelling of supported vs recommended, violation loop, bounded rounds, correction propagation and undo, flag answers, failure and resume without duplicate calls, session isolation, reset, grounding, double start, edits while running, input validation, every rule in the rule engine, retry behaviour of the HTTP client, and the one-shot repair of malformed model output.

## Approximate cost per run

Rough estimate, not measured on a real provider in this build: a clean run with the sample transcript is about 11k input and 4.5k output tokens across three calls, which is roughly $0.01 on the default model prices. A run with one review loop or one correction costs about twice that. The interface shows the actual token counts and an estimate for every run, so check it after your first real run and adjust `PRICE_PER_M_*` to your provider.

## Known limitations

- Real model output was not exercised in this build. Everything was checked with a scripted model and an HTTP stub, so expect to tune the prompts after the first live runs.
- Work runs in a background thread of the web process. A restart while a run is in progress marks it failed; it can be resumed, but there is no job queue or multi-worker support.
- SQLite on a host with an ephemeral disk loses data on redeploy. Mount a persistent volume on `DB_PATH`.
- Sessions are an anonymous random ID in the browser, not authentication. Anyone who knows a session ID and a run ID can read that run.
- The rule engine knows the seven sample rules by name (`R1` to `R7`). Rules R2 and R7 are enforced by the models, not by code. Custom rules in the JSON box are given to the models but only the built-in checks are enforced in code.
- Dates assume one timezone and working days of Monday to Friday; there is no holiday calendar.
- A correction replaces a fact in later steps. The original transcript is still what the Review Agent reads, so it is told that corrected facts are authoritative.
- Per session limits: 25 runs, 2 running at a time, transcripts up to 12,000 characters.

## Bugs found while building

- The first version of the feedback handoff view printed `[object HTMLDivElement]`: nested arrays of DOM nodes were only flattened one level. Fixed by flattening fully.
- Saving a correction form sent every field, so unchanged fields showed up as "Meera → Meera" in the change summary. The server now stores only fields that differ from the extracted fact and treats an all-equal edit as no change.

## Use of AI tools

Built with the help of an AI coding assistant. The code and design were reviewed and are explained in the walkthrough video.
