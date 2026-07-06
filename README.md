# Open Engine

A local, dependency-free dashboard that runs a task through a fully autonomous
AI agent pipeline — **Claude plans it, Antigravity implements it, Claude
reviews the implementation** — with one button, a bounded retry loop when the
review finds problems, and a human approval gate as the only manual step,
reserved for when the pipeline genuinely can't resolve something on its own.

Every plan, implementation run, review verdict, and routing decision is
recorded in SQLite, so the whole run stays inspectable after the fact — you
can always see what was planned, what Antigravity actually produced, and why
the reviewer approved or rejected it.

## Design philosophy

**Open Engine Design Architecture (Nate B. Jones Framework)**

Core invariant:

$$\text{Multi-Agent Handoff State} = \big[\text{Durable Context} + \text{State Receipts} + \text{Stop Points} + \text{Human Verification Layer}\big]$$

The Open Engine design (attributed to AI strategist Nate B. Jones) is an
architectural blueprint for solving the "human-as-a-hallway" bottleneck in
multi-agent workflows: when several independent AI systems (e.g. Claude Code,
Codex, custom LLM endpoints) hand a task between each other, execution state
typically gets lost between isolated chat sessions, forcing a human to
manually relay context from one tab to the next. This pattern moves the
orchestration layer away from stateful, vendor-locked chat sessions toward a
local-first, decoupled, task-record architecture — every handoff is a durable
row in SQLite, not a message in someone's chat history.

### Honest assessment of this codebase against that pattern

This v1 engine is currently a highly functional custom script dressed as an
enterprise framework. The framework components (ports/adapters, state
management) are intact, but they contain clusters of technical debt and tight
coupling that don't fully trace the pure decoupling concept the Open Engine
pattern calls for — most notably, the adapters in `adapters/` are still bound
directly to specific vendor CLIs (`claude`, `agy`) rather than an abstract
capability interface, so swapping in a different LLM or execution backend
still means writing a new adapter, not just changing configuration.

## Quick start

**Double-click [`start-server.bat`](start-server.bat)**, or run manually:

```
python server.py [port]      # default port: 8000
```

Then open `http://localhost:<port>` in a browser. On first load you'll be
asked to pick a project folder — tasks, agent runs, and artifacts are scoped
to it, and it's also the directory Antigravity actually implements into.

### Requirements

- Python 3.8+ (backend is stdlib-only: `http.server`, `sqlite3`, `subprocess`
  — nothing to `pip install`)
- A modern browser
- `claude` and `agy` (the **Antigravity CLI** — a separate headless agent
  binary, not the Antigravity IDE, which has no non-interactive mode) on
  `PATH`. The dashboard's Phase 0 probe reports whether each is installed and
  currently usable; if either is missing, the corresponding stage HOLDs
  instead of failing silently.

## The pipeline

Click **▶ Run Task (Delegate Routing)** on a task and the backend chains
three stages to completion on its own — no per-stage buttons, no picking an
agent:

1. **PLANNING (Claude)** — reads the task's title and description and
   produces a structured work order (summary, implementation steps, risk
   level, recommended action).
2. **EXECUTION (Antigravity)** — implements that work order directly against
   the selected project's files.
3. **REVIEW (Claude)** — inspects what Antigravity actually changed (not just
   its stdout summary) and returns `approved` or `changes_requested` with
   feedback.

If REVIEW asks for changes, the pipeline loops back to EXECUTION automatically
with the reviewer's feedback folded into the work order, for up to **3 review
cycles**. If it's still not approved after that, the task is marked `blocked`
and a **review decision** approval gate is created — the one point where a
human has to make a call: Approve to accept the implementation as-is, or
Reject to leave it blocked for manual follow-up. (PLANNING only ever runs
once per task, so a task stuck this way needs a new task with a clearer
description, not a re-plan.)

Each stage's routing is configured in [`config/routing.json`](config/routing.json):

```json
{
  "PLANNING":  { "primary": "claude",      "fallback": null, "degraded_allowed": false },
  "EXECUTION": { "primary": "antigravity", "fallback": null, "degraded_allowed": false },
  "REVIEW":    { "primary": "claude",      "fallback": null, "degraded_allowed": false }
}
```

None of the three stages has a fallback agent or a degraded (skip) mode — if
a stage's agent is unavailable or over quota, `RoutingPolicyService`
(`domain/routing_policy.py`) declares a **HOLD** rather than guessing. HOLD
and the exhausted-retries gate are the only two ways a task stops short of
`completed`; both are visible in Focus Mode and, for the gate case, actionable
from Approval Gates.

## Dashboard layout

- **01 — Command Center**: project selector, Phase 0 capability probe, task
  list, new-task form.
- **02 — Focus Mode**: the single "Run Task" button, a Plan → Execute →
  Review stepper, a status banner that reflects the task's actual state
  (done, needs a human, blocked, re-executing, etc.), and a live log that
  fills the rest of the panel — pushing the plan summary, review verdicts,
  and Antigravity's own output as each stage completes.
- **03 — Atomic Review**: Approval Gates (only populated when a task is
  genuinely stuck), the Work Order Claude produced, Artifacts (Antigravity's
  captured output per task, expandable inline or opened in any app via
  Windows' native "Open with" picker), and an Audit Trail of every agent run.

## Manual hand-off fallback

If Antigravity's CLI can't be invoked at all (not installed, fails to start,
times out), the adapter writes an inbox file to `handoff-lab/` describing the
work order and asking a human to process it and drop a return artifact back
in that directory. The dashboard's poll endpoint
(`GET /api/run/<task_id>/poll`) picks it up automatically. This is
EXECUTION's only safety net, since it has no fallback agent.

## Project structure

```
server.py             stdlib HTTP server — routes, SSE broadcast, DB access
application/          OrchestrationService — stage routing, payload
                       resolution between stages, review-retry/gate logic
domain/                Agent/Role enums, capability model, routing policy
adapters/              claude_cli.py (planner + reviewer), antigravity_cli.py
                       (executor + manual hand-off), quota classifier,
                       SQLite capability store, SSE notifier, capability probe
ports/                 Interfaces the adapters implement
config/routing.json    Per-stage primary-agent routing matrix
db_schema.sql          SQLite schema (tasks, agent_runs, artifacts,
                       approval_gates, routing_decisions, active_project, ...)
index.html             Dashboard shell
src/                   Frontend: state.js, api.js, ui.js, styles.css
handoff-lab/           Manual hand-off inbox/return-artifact directory
artifact_exports/      Per-task subfolders of artifact content exported via
                       the "Open with" picker (task_<id>/<artifact_id>_name)
verify_handoff.py      Test suite (run with `python verify_handoff.py`)
```

## API reference

```
GET  /                               serve index.html
GET  /src/*                          static frontend files
GET  /api/tasks                      list tasks (optionally ?project=<path>)
POST /api/tasks                      create task
GET  /api/tasks/<id>                 task detail + agent runs
POST /api/telemetry                  append telemetry event
GET  /api/events                     SSE stream (live log, task/approval updates, ...)
POST /api/approval                   approve/reject a gate
GET  /api/approval                   list pending gates
POST /api/artifacts                  ingest a return artifact (used by the manual hand-off poll)
GET  /api/artifacts/<task_id>        list artifacts for a task
POST /api/artifacts/<id>/open        export an artifact's content to artifact_exports/task_<id>/
                                      and open Windows' native "Open with" app picker
POST /api/probe                      run Phase 0 capability probe
GET  /api/probe                      last probe results
GET  /api/run/<task_id>/poll         poll for Antigravity return artifacts (manual hand-off)
POST /api/tasks/<task_id>/delegate   the only "run a task" action - chains PLANNING ->
                                      EXECUTION -> REVIEW to completion automatically,
                                      looping back to EXECUTION on changes_requested
                                      (capped retries), stopping on HOLD or an
                                      exhausted-retries approval gate
GET  /api/project                    currently selected project folder (or null)
POST /api/project                    select a project folder: {path, name?}
GET  /api/fs/list?path=<path>        in-app folder browser (folders only); omit path
                                      for drive/root list
POST /api/capability/reset           clear a stuck EXHAUSTED/cooldown state: {agent}
```

## Data

State lives in `db.sqlite` (schema in `db_schema.sql`) — tasks, agent runs,
artifacts, approval gates, routing decisions, and the currently selected
project are all persisted there, so the dashboard survives a server restart.
`routing_decisions` is still recorded on every stage for auditing (queryable
directly via SQLite) even though it's no longer shown in the dashboard UI.
