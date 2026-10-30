const state = { session: null, runId: null, snapshot: "", timer: null, openKeys: new Set(), flashTimer: null };

const AGENTS = {
  intake: { title: "Intake Agent", role: "Reads the transcript, extracts facts with line references, flags gaps and conflicts" },
  planning: { title: "Planning Agent", role: "Builds tasks from the facts and company rules, separating support from recommendation" },
  review: { title: "Review Agent", role: "Audits the plan against the transcript and rules, sends corrections back" },
};

const STATUS_TEXT = {
  queued: "Queued",
  running: "Running",
  failed: "Failed, can resume",
  complete: "Complete",
  complete_with_issues: "Stopped with open issues",
  idle: "Waiting",
  done: "Done",
};

function newId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "s" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

function loadSession() {
  try {
    let saved = localStorage.getItem("ops_session");
    if (!saved) {
      saved = newId();
      localStorage.setItem("ops_session", saved);
    }
    return saved;
  } catch (e) {
    return newId();
  }
}

function saveSession(id) {
  try {
    localStorage.setItem("ops_session", id);
  } catch (e) {
    return;
  }
}

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const $ = (id) => document.getElementById(id);

async function api(path, options = {}, session = state.session) {
  const res = await fetch("/api" + path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Session-Id": session, ...(options.headers || {}) },
  });
  let body = null;
  try {
    body = await res.json();
  } catch (e) {
    body = null;
  }
  if (!res.ok) {
    let message = res.statusText;
    if (body && body.detail) {
      message = typeof body.detail === "string" ? body.detail : body.detail.map((d) => d.msg).join("; ");
    }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return body;
}

function toast(message, bad) {
  const box = $("toast");
  box.textContent = message;
  box.className = "toast" + (bad ? " bad" : "");
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 5000);
}

function clock(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("en-IN", { hour12: false });
}

function dayLabel(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

function pill(status) {
  return el("span", { class: "pill " + status }, STATUS_TEXT[status] || status);
}

function tag(kind, text) {
  return el("span", { class: "tag " + kind }, text || kind);
}

function json(value) {
  return JSON.stringify(value, null, 2);
}

function fold(key, label, value) {
  const node = el("details", { "data-key": key }, el("summary", {}, label), el("pre", {}, json(value)));
  if (state.openKeys.has(key)) node.open = true;
  node.addEventListener("toggle", () => {
    if (node.open) state.openKeys.add(key);
    else state.openKeys.delete(key);
  });
  return node;
}

function refs(list, lines) {
  return (list || []).map((label) => {
    const line = (lines || []).find((l) => l.label === label);
    return el("button", {
      class: "chip",
      type: "button",
      title: line ? line.text : label,
      onclick: () => jumpTo(label),
    }, label);
  });
}

function jumpTo(label) {
  const node = document.getElementById("line-" + label);
  if (!node) return;
  const box = node.closest("details");
  if (box && !box.open) box.open = true;
  node.scrollIntoView({ block: "center", behavior: "smooth" });
  node.classList.add("flash");
  clearTimeout(state.flashTimer);
  state.flashTimer = setTimeout(() => node.classList.remove("flash"), 1800);
}

async function loadSample() {
  const sample = await api("/sample");
  $("transcript").value = sample.transcript;
  $("rules").value = json(sample.rules);
  document.querySelectorAll(".faults input").forEach((box) => { box.checked = false; });
}

async function refreshRuns() {
  const runs = await api("/runs");
  const list = $("runs");
  list.replaceChildren();
  if (!runs.length) {
    list.append(el("li", { class: "muted small" }, "Nothing here yet in this session."));
    return;
  }
  for (const run of runs) {
    list.append(el("li", {},
      el("button", {
        type: "button",
        class: run.id === state.runId ? "active" : "",
        onclick: () => openRun(run.id),
      },
        el("span", { class: "t" }, run.title || run.id),
        el("span", { class: "m" }, el("span", {}, run.id), pill(run.status)),
      )));
  }
}

async function startRun() {
  let rules;
  try {
    rules = JSON.parse($("rules").value);
  } catch (e) {
    toast("The company rules box is not valid JSON", true);
    return;
  }
  const faults = [...document.querySelectorAll(".faults input:checked")].map((b) => b.value);
  $("start").disabled = true;
  try {
    const out = await api("/runs", {
      method: "POST",
      body: JSON.stringify({ transcript: $("transcript").value, rules, faults }),
    });
    await openRun(out.id);
    toast("Run " + out.id + " started");
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("start").disabled = false;
  }
}

async function openRun(id) {
  state.runId = id;
  state.snapshot = "";
  await poll();
  refreshRuns().catch(() => {});
}

async function poll() {
  clearTimeout(state.timer);
  if (!state.runId) return;
  try {
    const data = await api("/runs/" + state.runId);
    const snap = JSON.stringify(data);
    if (snap !== state.snapshot) {
      state.snapshot = snap;
      draw(data);
      refreshRuns().catch(() => {});
    }
    if (["queued", "running"].includes(data.run.status)) {
      state.timer = setTimeout(poll, 1500);
    }
  } catch (err) {
    toast(err.message, true);
  }
}

async function act(path, body, okMessage) {
  try {
    await api(path, { method: "POST", body: JSON.stringify(body || {}) });
    if (okMessage) toast(okMessage);
    state.snapshot = "";
    await poll();
  } catch (err) {
    toast(err.message, true);
  }
}

function headBlock(d) {
  const run = d.run;
  const parts = [
    el("div", { class: "head" },
      el("div", {},
        el("h2", {}, run.title || run.id),
        el("div", { class: "row" }, pill(run.status), el("span", { class: "muted small" }, "Run " + run.id),
          el("span", { class: "muted small" }, "Context v" + d.context.version)),
      ),
      el("div", { class: "row" },
        run.status === "failed"
          ? el("button", { class: "primary", onclick: () => act("/runs/" + run.id + "/resume", {}, "Resuming from the failed step") }, "Resume failed step")
          : null,
      ),
    ),
    el("div", { class: "stats" },
      el("div", {}, el("span", { class: "muted" }, "Review rounds"), el("b", {}, (d.result ? d.result.rounds : Math.max(d.agents.review.attempts, 0)) + " of " + run.max_rounds)),
      el("div", {}, el("span", { class: "muted" }, "Tokens in / out"), el("b", {}, d.cost.tokens_in + " / " + d.cost.tokens_out)),
      el("div", {}, el("span", { class: "muted" }, "Approx. cost"), el("b", {}, "$" + d.cost.usd.toFixed(4))),
      el("div", {}, el("span", { class: "muted" }, "Context versions"), el("b", {}, d.contexts.length)),
    ),
  ];
  if (run.faults.length) {
    parts.push(el("div", { class: "note sim" }, el("b", {}, "Armed simulated faults: "), run.faults.join(", ")));
  }
  if (run.note) {
    parts.push(el("div", { class: "note " + (run.status === "failed" ? "bad" : "warn") },
      run.failed_step ? el("b", {}, "Stopped at " + run.failed_step + ". ") : null, run.note));
  }
  return el("section", { class: "block" }, parts);
}

function attemptView(step) {
  const key = "step-" + step.id;
  const children = [
    el("div", { class: "line" },
      el("b", {}, "Attempt " + step.attempt),
      pill(step.status),
      el("span", { class: "muted" }, "v" + step.context_version),
      step.simulated ? tag("sim", "simulated fault") : null,
      step.retries ? el("span", { class: "muted" }, "retried " + step.retries + "x") : null,
    ),
  ];
  if (step.tokens_in || step.tokens_out) {
    children.push(el("div", { class: "muted small" }, step.tokens_in + " in, " + step.tokens_out + " out tokens"));
  }
  if (step.error) children.push(el("div", { class: "err" }, step.error));
  if (step.notes.length) children.push(el("ul", {}, step.notes.map((n) => el("li", {}, n))));
  children.push(fold(key + "-in", "What it received", step.input));
  if (step.output) children.push(fold(key + "-out", "What it produced", step.output));
  return el("div", { class: "attempt" }, children);
}

function pipelineBlock(d) {
  const cards = Object.keys(AGENTS).map((name) => {
    const info = d.agents[name];
    const steps = d.steps.filter((s) => s.agent === name).slice(-4).reverse();
    return el("div", { class: "agent " + info.status },
      el("header", {}, el("h3", {}, AGENTS[name].title), pill(info.status)),
      el("div", { class: "role" }, AGENTS[name].role),
      el("div", { class: "muted small" }, info.attempts + (info.attempts === 1 ? " attempt" : " attempts") + " so far"),
      steps.map(attemptView),
    );
  });
  return el("section", { class: "block" }, el("h2", {}, "Agents"), el("div", { class: "pipeline" }, cards));
}

function handoffView(h) {
  const key = "handoff-" + h.id;
  const body = [];
  if (h.kind === "feedback") {
    body.push(h.payload.issues.map((i) => issueView(i)));
  } else if (h.kind === "facts") {
    body.push(el("div", { class: "small muted" },
      h.payload.facts.length + " facts, " + h.payload.open_flags.length + " open flags, " +
      h.payload.resolutions.length + " user answers passed to planning"));
  } else {
    body.push(el("div", { class: "small muted" }, h.payload.plan.tasks.length + " proposed tasks sent for review"));
  }
  body.push(fold(key, "Raw message", h.payload));
  return el("div", { class: "handoff " + (h.valid ? h.kind : "bad") },
    el("div", { class: "line" },
      el("b", {}, h.from), el("span", { class: "arrow" }, "→"), el("b", {}, h.to),
      tag("none", h.kind),
      h.valid ? tag("stated", "schema valid") : tag("conflict", "rejected"),
      el("span", { class: "muted" }, "v" + h.context_version + " · " + clock(h.created_at)),
    ),
    h.issues.length ? el("div", { class: "issue" }, h.issues.join("; ")) : null,
    body,
  );
}

function issueView(issue) {
  return el("div", { class: "issue" },
    tag(issue.severity),
    " ",
    issue.source === "rule-engine" ? tag("engine", "rule engine") : null,
    issue.source === "rule-engine" ? " " : null,
    el("b", {}, [issue.task_id, issue.rule_id].filter(Boolean).join(" · ") || "General"),
    " ",
    issue.problem,
    el("div", { class: "fix" }, "Correction: " + issue.correction),
  );
}

function handoffBlock(d) {
  const body = d.handoffs.length
    ? d.handoffs.map(handoffView)
    : [el("p", { class: "muted small" }, "Handoff messages appear here as agents pass work along.")];
  return el("section", { class: "block" },
    el("h2", {}, "Handoffs and feedback"),
    el("p", { class: "muted small" }, "Every message between agents is validated against a schema before the receiving agent runs."),
    body,
  );
}

function correctionForm(fact, d) {
  const form = el("form", {
    class: "inline-form",
    onsubmit: (ev) => {
      ev.preventDefault();
      const data = new FormData(form);
      const payload = { fact_id: fact.id, text: data.get("text"), owner: data.get("owner"), deadline_text: data.get("deadline_text") };
      act("/runs/" + d.run.id + "/corrections", payload, "Correction saved, affected steps are re-running");
    },
  },
    el("label", {}, "Statement", el("input", { type: "text", name: "text", value: fact.text })),
    el("div", { class: "grid" },
      el("label", {}, "Owner", el("input", { type: "text", name: "owner", value: fact.owner || "", placeholder: "none" })),
      el("label", {}, "Deadline as stated", el("input", { type: "text", name: "deadline_text", value: fact.deadline_text || "", placeholder: "none" })),
    ),
    el("div", { class: "row" },
      el("button", { class: "primary small-btn", type: "submit" }, "Save and rerun"),
      fact.corrected
        ? el("button", { class: "small-btn", type: "button", onclick: () => act("/runs/" + d.run.id + "/corrections", { fact_id: fact.id, revert: true }, "Correction undone") }, "Undo correction")
        : null,
    ),
  );
  return form;
}

function factsBlock(d) {
  const result = d.result;
  const intakeStep = [...d.steps].reverse().find((s) => s.agent === "intake" && s.status === "done");
  if (!intakeStep) return null;
  const busy = ["queued", "running"].includes(d.run.status);
  const facts = result ? result.facts : applyLocal(intakeStep.output.facts, d.context.corrections);
  const flags = intakeStep.output.flags;
  const answered = d.context.resolutions;
  const factRows = facts.map((f) => {
    const slot = el("div", { hidden: true }, correctionForm(f, d));
    const row = el("tr", { class: f.corrected ? "corrected" : "" },
      el("td", {}, f.id),
      el("td", {}, tag("none", f.kind)),
      el("td", {}, f.text, f.corrected ? el("span", {}, " ", tag("corrected", "corrected by you")) : null, slot),
      el("td", {}, refs(f.source_refs, d.lines)),
      el("td", {}, f.owner || el("span", { class: "muted" }, "none")),
      el("td", {}, f.deadline_text || el("span", { class: "muted" }, "none")),
      el("td", {}, el("button", {
        class: "small-btn", type: "button", disabled: busy,
        onclick: () => { slot.hidden = !slot.hidden; },
      }, "Correct")),
    );
    return row;
  });

  const flagRows = flags.map((f) => {
    const done = answered[f.id];
    const form = el("form", {
      class: "inline-form",
      onsubmit: (ev) => {
        ev.preventDefault();
        const answer = new FormData(form).get("answer");
        act("/runs/" + d.run.id + "/resolutions", { flag_id: f.id, answer }, "Answer saved, affected steps are re-running");
      },
    },
      el("input", { type: "text", name: "answer", placeholder: "Your answer", required: true, value: done || "" }),
      el("div", {}, el("button", { class: "small-btn", type: "submit", disabled: busy }, done ? "Update answer" : "Answer and rerun")),
    );
    return el("div", { class: "flag" },
      el("div", { class: "line" }, tag(f.kind), el("b", {}, f.id), refs(f.source_refs, d.lines), done ? tag("stated", "answered") : tag("recommended", "open")),
      el("div", {}, f.text),
      done ? el("div", { class: "small muted" }, "Your answer: " + done) : null,
      form,
    );
  });

  return el("section", { class: "block" },
    el("h2", {}, "What the Intake Agent extracted"),
    el("p", { class: "muted small" }, "Click a line reference to jump to the original transcript line. Correcting a fact creates a new context version and only the steps that depend on it run again."),
    el("div", { class: "scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["ID", "Type", "Statement", "Source", "Owner", "Deadline said", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, factRows))),
    el("h3", {}, "Missing or conflicting information"),
    flagRows.length ? flagRows : el("p", { class: "muted small" }, "None flagged."),
  );
}

function applyLocal(facts, corrections) {
  return facts.map((f) => {
    const patch = corrections[f.id];
    if (!patch) return f;
    const copy = { ...f, corrected: true };
    for (const [k, v] of Object.entries(patch)) {
      if (v == null) delete copy[k];
      else copy[k] = v;
    }
    return copy;
  });
}

function taskRows(tasks, d, affected) {
  return tasks.map((t) => el("tr", { class: affected.has(t.id) ? "affected" : "" },
    el("td", {}, t.id),
    el("td", {}, t.title, el("span", { class: "sub" }, t.rationale)),
    el("td", {}, t.owner || el("span", { class: "muted" }, "unassigned"), el("span", { class: "sub" }, tag(t.owner_basis))),
    el("td", {}, t.deadline ? dayLabel(t.deadline) : el("span", { class: "muted" }, "none"), el("span", { class: "sub" }, tag(t.deadline_basis))),
    el("td", {}, t.depends_on.length ? t.depends_on.join(", ") : el("span", { class: "muted" }, "none")),
    el("td", {}, (t.supported_by || []).length ? t.supported_by.join(", ") : el("span", { class: "muted" }, "none")),
  ));
}

function taskTable(tasks, d, affected) {
  return el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ["ID", "Task", "Owner", "Deadline", "After", "Facts"].map((h) => el("th", {}, h)))),
    el("tbody", {}, taskRows(tasks, d, affected))));
}

function planBlock(d) {
  if (!d.result) return null;
  const { plan, review } = d.result;
  const affected = new Set(Object.values(d.propagation.affected_tasks).flat());
  const supported = plan.tasks.filter((t) => t.basis === "supported");
  const recommended = plan.tasks.filter((t) => t.basis !== "supported");
  const parts = [
    el("h2", {}, "Final plan"),
    plan.summary ? el("p", {}, plan.summary) : null,
  ];
  if (d.result.unresolved_issues.length) {
    parts.push(el("div", { class: "note warn" },
      el("b", {}, "The review loop stopped with these issues still open"),
      d.result.unresolved_issues.map((i) => issueView(i))));
  }
  parts.push(el("h3", {}, "Supported by the transcript"));
  parts.push(supported.length ? taskTable(supported, d, affected) : el("p", { class: "muted small" }, "No task is fully supported by the transcript."));
  parts.push(el("h3", {}, "Recommendations (not stated in the meeting)"));
  parts.push(recommended.length ? taskTable(recommended, d, affected) : el("p", { class: "muted small" }, "None."));
  parts.push(el("h3", {}, "Unresolved questions"));
  const open = plan.unresolved_questions || [];
  parts.push(open.length
    ? el("ul", {}, open.map((q) => el("li", {}, q.question, " ", (q.related || []).map((r) => tag("none", r)))))
    : el("p", { class: "muted small" }, "None."));
  if (plan.changes_made && plan.changes_made.length) {
    parts.push(el("h3", {}, "Changes made after review"));
    parts.push(el("ul", {}, plan.changes_made.map((c) => el("li", {}, c))));
  }
  if (review.notes) parts.push(el("p", { class: "muted small" }, "Reviewer note: " + review.notes));
  const minor = review.issues.filter((i) => i.severity === "minor");
  if (minor.length) {
    parts.push(el("h3", {}, "Minor review remarks"));
    parts.push(minor.map((i) => issueView(i)));
  }
  return el("section", { class: "block" }, parts);
}

function impactBlock(d) {
  const p = d.propagation;
  if (!p.corrections.length && !p.changes.length) return null;
  const parts = [el("h2", {}, "Effect of your corrections")];
  if (p.corrections.length) {
    parts.push(el("ul", { class: "changes" }, p.corrections.map((c) => el("li", {},
      el("b", {}, c.fact_id), ": ", c.fields.map((f) => f.field.replace("_", " ") + " " +
        (f.before == null ? "none" : "“" + f.before + "”") + " → " + (f.after == null ? "none" : "“" + f.after + "”")).join("; "),
      d.result ? el("span", { class: "muted" }, " · tasks built on it: " + ((p.affected_tasks[c.fact_id] || []).join(", ") || "none")) : null))));
  }
  if (p.changes.length) {
    parts.push(el("h3", {}, "Plan before and after"));
    parts.push(el("ul", { class: "changes" }, p.changes.map((c) => el("li", {},
      el("b", {}, c.task_id), " ", c.field, ": ",
      el("span", { class: "arrow-txt" }, c.before == null ? "new" : c.field === "deadline" ? dayLabel(c.before) : c.before),
      " → ",
      c.after == null ? "removed" : c.field === "deadline" ? dayLabel(c.after) : c.after))));
  }
  parts.push(el("p", { class: "muted small" }, "The Intake Agent output was reused from the saved step. Planning and Review ran again on context v" + d.context.version + "."));
  return el("section", { class: "block" }, parts);
}

function transcriptBlock(d) {
  const node = el("details", { "data-key": "transcript" },
    el("summary", {}, "Original transcript (" + d.lines.length + " lines)"),
    el("div", { class: "transcript" }, d.lines.map((l) => el("div", { class: "tline", id: "line-" + l.label },
      el("span", { class: "n" }, l.label), el("span", {}, l.text)))));
  if (state.openKeys.has("transcript")) node.open = true;
  node.addEventListener("toggle", () => {
    if (node.open) state.openKeys.add("transcript");
    else state.openKeys.delete("transcript");
  });
  return el("section", { class: "block" }, node);
}

function logBlock(d) {
  const box = el("div", { class: "log" }, d.events.map((e) => el("div", { class: e.level },
    el("span", { class: "time" }, clock(e.created_at)), e.message)));
  setTimeout(() => { box.scrollTop = box.scrollHeight; }, 0);
  return el("section", { class: "block" }, el("h2", {}, "Activity log"), box);
}

function isolationBlock(d) {
  const out = el("div", { class: "small muted" }, "Sessions never share runs, facts or agent context. Try opening this run as someone else.");
  const button = el("button", {
    type: "button",
    onclick: async () => {
      const stranger = newId();
      const lines = [];
      try {
        await api("/runs/" + d.run.id, {}, stranger);
        lines.push("Unexpected: the other session could read this run");
      } catch (err) {
        lines.push("Fetching run " + d.run.id + " from a different session returned HTTP " + err.status + ": " + err.message);
      }
      try {
        const theirs = await api("/runs", {}, stranger);
        lines.push("That session lists " + theirs.length + " runs of its own.");
      } catch (err) {
        lines.push(err.message);
      }
      out.replaceChildren(...lines.map((l) => el("div", {}, l)));
    },
  }, "Open this run from another session");
  return el("section", { class: "block" }, el("h2", {}, "Session isolation"), el("div", { class: "row" }, button), out);
}

function draw(d) {
  const detail = $("detail");
  const scrollTop = window.scrollY;
  const blocks = [
    headBlock(d),
    pipelineBlock(d),
    handoffBlock(d),
    factsBlock(d),
    planBlock(d),
    impactBlock(d),
    transcriptBlock(d),
    logBlock(d),
    isolationBlock(d),
  ].filter(Boolean);
  detail.replaceChildren(...blocks);
  window.scrollTo(0, scrollTop);
}

async function boot() {
  state.session = loadSession();
  $("session-id").textContent = state.session.slice(0, 8);
  $("start").addEventListener("click", startRun);
  $("load-sample").addEventListener("click", () => loadSample().catch((e) => toast(e.message, true)));
  $("new-session").addEventListener("click", () => {
    state.session = newId();
    saveSession(state.session);
    state.runId = null;
    state.snapshot = "";
    $("session-id").textContent = state.session.slice(0, 8);
    $("detail").replaceChildren(el("div", { class: "empty" }, el("h2", {}, "New session"), el("p", {}, "This session has no runs and cannot see any from before.")));
    refreshRuns();
    toast("Switched to a new session");
  });
  $("reset").addEventListener("click", async () => {
    if (!confirm("Delete every run in this session and restore the sample input?")) return;
    try {
      const out = await api("/reset", { method: "POST" });
      state.runId = null;
      state.snapshot = "";
      $("detail").replaceChildren(el("div", { class: "empty" }, el("h2", {}, "Reset done"), el("p", {}, out.removed + " runs removed.")));
      await loadSample();
      await refreshRuns();
      toast("Reset complete" + (out.skipped_running ? ", " + out.skipped_running + " running run kept" : ""));
    } catch (err) {
      toast(err.message, true);
    }
  });

  try {
    const cfg = await api("/config");
    if (!cfg.llm_ready) {
      const banner = $("banner");
      banner.hidden = false;
      banner.textContent = "No model API key is configured on the server, so runs will fail at the first agent. Set LLM_API_KEY and restart.";
    }
  } catch (err) {
    toast(err.message, true);
  }

  await loadSample();
  await refreshRuns();
}

boot();
