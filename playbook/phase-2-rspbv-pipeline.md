# Phase 2 — RSPBV pipeline (Research, Spec, Plan, Build, Verify)

```json
{
  "work_order_id": "OE-V2-PHASE2-RSPBV",
  "task_summary": "Expand the existing 3-stage pipeline (PLANNING -> EXECUTION -> REVIEW) into the 5-stage RSPBV pipeline (RESEARCH -> SPEC -> PLANNING -> EXECUTION -> REVIEW): RESEARCH queries the Phase 1 memory store with no LLM call, SPEC is a new Claude-authored acceptance-criteria stage grounded in that research, and REVIEW gains an optional real verify-command run plus a wisdom write-back to memory on approval.",
  "recommended_action": "proceed"
}
```

## Prerequisite

**Phase 1 must be complete, tested, and merged first.** This phase imports
and depends on `ports/memory_store.py` and `adapters/sqlite_memory.py`. Do
not start this phase's implementation if those don't exist yet and pass
their own tests.

## Context (read this before writing any code)

**Why the CLI adapters stay, not a direct API call:** an earlier draft of
this evolution considered dropping the `claude` CLI for planning in favor of
a direct HTTP API call, reasoning that it would make attaching memory
context easier. That reasoning doesn't hold — retrieved memory is just text,
and prepending it to the CLI's stdin prompt works identically to putting it
in an API request body — and it directly contradicts this project's stated
final intent: **an autonomous implementation pipeline built on the `claude`
and `antigravity` CLI adapters.** The CLI also provides things a raw API
call doesn't without reimplementing an agent loop: subscription-based auth
(no separate API key/billing) and agentic repo reading via `--add-dir`
(which Phase 2 depends on — see the `cwd=`/`--add-dir` note below). Do not
remove or bypass `adapters/claude_cli.py` in this phase.

**Read these existing files in full before changing anything** — this phase
extends established machinery, it does not replace it:
- `domain/agent.py` — `Role` and `AgentName` enums.
- `config/routing.json` — the per-role routing matrix shape.
- `application/orchestration_service.py` — **all of it**, especially
  `run_leg()`, `_auto_resolve_payload()`, and `next_role()`. These three
  functions are what you're extending; the new stages must follow the exact
  patterns already used for PLANNING/EXECUTION/REVIEW, not a new shape.
  `next_role()` in particular walks `agent_runs` rows by role/status/id
  ordering to figure out what's next — RESEARCH and SPEC slot into that
  same walk, before the existing PLANNING check.
- `adapters/claude_cli.py` — **all of it**. Note in particular:
  - `_run_claude_cli(prompt, project_dir=None)` already accepts a
    `project_dir` and passes it both as `--add-dir` *and* as the subprocess
    `cwd` (a fix landed recently after discovering `--add-dir` alone does
    not redirect Claude away from the server process's own working
    directory — it only grants supplementary access, it doesn't replace the
    primary project context). Any new Claude role you add (SPEC) must use
    this same function with `project_dir` set, the same way `_invoke_as_reviewer`
    does — do not invoke `subprocess` directly or add a second code path.
  - `_parse_claude_json(raw)` — reuse this exactly for parsing SPEC's
    response. Do not write a second JSON-extraction routine.
  - `_invoke_as_planner`/`_invoke_as_reviewer` — structural template for the
    new `_invoke_as_spec` method: build prompt -> call `_run_claude_cli` ->
    parse -> validate -> insert `agent_runs` row -> return dict.
- `adapters/antigravity_cli.py` — note the equivalent `cwd=project_dir` fix
  is also already present here; don't remove it.
- `server.py::_migrate_schema` — the CHECK-constraint-widening table-rebuild
  pattern (used previously to add `'quota_exceeded'`/`'skipped_degraded'` to
  `agent_runs.status`) is what you'll reuse to add `'engine'` to
  `agent_runs.agent_name`. Read the comments in that function carefully —
  there's a documented gotcha about `approval_gates.agent_run_id`'s foreign
  key getting corrupted by SQLite's default `ALTER TABLE RENAME` behavior,
  worked around with `PRAGMA legacy_alter_table=ON`. Follow that same
  workaround, don't rediscover it the hard way.
- `server.py::_create_review_gate` (inside `OrchestrationService`, actually
  — check current location) — the dedup-guard pattern (checking whether a
  gate already exists before creating another) is the pattern to follow for
  guarding wisdom write-back against double-writes.

## Implementation steps

1. **`domain/agent.py`**: add `RESEARCH` and `SPEC` to the `Role` enum, in
   this position: `RESEARCH, SPEC, PLANNING, EXECUTION, REVIEW`. Add
   `ENGINE` to `AgentName` (represents the orchestrator doing a direct
   memory query — not an LLM, not a CLI subprocess).

2. **`config/routing.json`**: add
   ```json
   "RESEARCH": { "primary": "engine", "fallback": null, "degraded_allowed": false },
   "SPEC":     { "primary": "claude", "fallback": null, "degraded_allowed": false },
   ```
   ahead of the existing `PLANNING` entry. Keep the existing "no fallback,
   no degraded mode, HOLD if unavailable" philosophy already established for
   every other stage in this file — don't introduce fallback logic here.

3. **Schema**: widen `agent_runs.agent_name` CHECK to
   `('claude','antigravity','engine')` — note `'codex'` was already removed
   from this constraint in an earlier cleanup; don't reintroduce it. Use the
   exact table-rebuild technique already in `_migrate_schema` for the
   `agent_runs.status` widening (rename → recreate → copy → drop, with
   `PRAGMA legacy_alter_table=ON` around the rename). Update
   `db_schema.sql`'s own `CREATE TABLE` too so a fresh install gets the
   right constraint from the start.

4. **`ports/memory_store.py` invoker adapter** — create a tiny class (put it
   in `adapters/sqlite_memory.py` or a new small file, your call, but it
   must implement the same interface as the CLI adapters —
   `invoke(self, task_id, role, db_path, payload) -> dict`, see
   `ports/agent_invocation.py`) that wraps `SQLiteMemoryStore`:
   ```python
   class MemoryResearchInvoker:
       def __init__(self, memory_store): ...
       def invoke(self, task_id, role, db_path, payload):
           # look up task title+description, call memory_store.search(...),
           # insert an agent_runs row (agent_name='engine', role='RESEARCH',
           # status='completed', logs=json of the retrieved snippets),
           # return {"snippets": [...]}
   ```
   Register it in `server.py`'s orchestrator-builder (wherever
   `AgentName.CLAUDE`/`AgentName.ANTIGRAVITY` are currently mapped to their
   adapters) under `AgentName.ENGINE`. **Prefer this over a special-cased
   branch inside `run_leg()`** — routing this through the normal
   invoker-dispatch machinery means RESEARCH gets HOLD/audit/routing-decision
   behavior for free, identically to every other role, instead of a
   parallel code path that has to be kept in sync by hand.

5. **`application/orchestration_service.py`**:
   - `_auto_resolve_payload`: SPEC needs the RESEARCH bundle (latest
     completed `engine`/`RESEARCH` run's logs); PLANNING needs both the
     RESEARCH bundle and the SPEC (latest completed `claude`/`SPEC` run's
     logs); EXECUTION needs `spec` added alongside the existing `work_order`
     resolution. Follow the exact existing style (SQL lookups scoped by
     `task_id`, ordered `DESC LIMIT 1`, wrapped in try/except around
     `json.loads`).
   - `next_role()`: insert checks for RESEARCH and SPEC before the existing
     PLANNING check, in the same "is there a completed run of role X with a
     higher id than the previous stage's run" style already used for
     EXECUTION/REVIEW.
   - Wisdom write-back: when a task reaches `completed` status (both the
     existing "REVIEW approved" path in `next_role()` and the
     "review_decision gate approved by a human" path in `server.py`'s
     approval handler), call `memory_store.write_wisdom(task_id, summary)`.
     Guard against writing twice for the same task (check for an existing
     wisdom document with that `task_id` first — same dedup-check shape
     already used for approval gates). Compose `summary` from: task title,
     work order's `task_summary`, review's `feedback`, and (if present) the
     verify command's output tail — keep it compact, a few hundred words,
     not a full dump.

6. **`adapters/claude_cli.py`**:
   - Add SPEC to the `invoke()` dispatch: `if role == "SPEC": return self._invoke_as_spec(...)`.
   - New `_invoke_as_spec(self, task_id, db_path, payload)`: same structural
     shape as `_invoke_as_planner`. Prompt includes the task title/description
     *and* the RESEARCH bundle from `payload["research"]`. Produces JSON:
     `{"spec_id": ..., "objective": ..., "acceptance_criteria": [...], "out_of_scope": [...], "files_expected": [...]}`.
     Validate the same way PLANNING validates `implementation_steps` — if
     `acceptance_criteria` is empty, raise, don't silently store a useless
     spec (this mirrors a real bug fixed in PLANNING earlier: an empty/
     unusable output must fail loudly, not be recorded as a "completed"
     success that later stages then choke on quietly).
   - Update `_invoke_as_planner`'s prompt to include both the RESEARCH
     bundle and the SPEC (via `payload`, same as reviewer already receives
     `work_order`/`execution_output` via payload) so the work order is
     grounded in both.

7. **`adapters/antigravity_cli.py`**: include `payload.get("spec")` in the
   prompt text sent to Antigravity, alongside the existing `work_order`, so
   the executor sees acceptance criteria, not just implementation steps.

8. **Verify command execution** (part of REVIEW/Verify): add an optional
   `verify_command TEXT` column to `tasks` (simple additive `ALTER TABLE`,
   same pattern as every other column addition in `_migrate_schema`). When
   set, run it as part of the REVIEW step — **implement this in
   `OrchestrationService`, not inside the Claude adapter** (running
   arbitrary subprocess commands is an orchestration concern, keep the
   Claude adapter focused on talking to Claude): `subprocess.run(shlex.split(verify_command), cwd=project_dir, capture_output=True, text=True, timeout=300)`,
   catch `subprocess.TimeoutExpired`, pass `{"verify_exit_code": ..., "verify_output": <last ~2000 chars of combined stdout+stderr>}` into the
   reviewer's payload. Update `_invoke_as_reviewer`'s prompt to include this
   section when present, clearly labeled (e.g. `## Verify Command Output`),
   so REVIEW has an objective pass/fail signal alongside its own subjective
   code read — and so it can no longer rationalize a no-op execution as
   "correct" the way an earlier bug allowed (a real failure mode found in
   testing: REVIEW approved a task where Antigravity produced no output at
   all, reasoning the task was "read-only" — a real verify command's exit
   code is a hard check against exactly that kind of false approval).

9. **Retry loop stays Build↔Verify, not Spec↔Verify**: if REVIEW requests
   changes, the existing bounded retry loop re-runs EXECUTION with the
   review's feedback folded into the work order (unchanged mechanism, same
   `REVIEW_RETRY_LIMIT`, same human-approval-gate escalation on exhaustion).
   Do not route a failed verify back to SPEC — that's a much more expensive
   re-plan than the situation usually calls for; a human can decide "this
   needs a new spec" at the gate if the retry budget is genuinely exhausted.

10. **Tests** (`verify_handoff.py`) — extend the existing
    `OrchestrationService`/`ClaudeCLIAdapter` test patterns:
    - `next_role()` walks a fresh task through RESEARCH → SPEC → PLANNING →
      EXECUTION → REVIEW in order (mirrors the existing
      `test_next_role_walks_planning_execution_review` test — extend it or
      add a new one covering the full 5-stage walk).
    - RESEARCH completes without any `subprocess`/CLI mock being invoked at
      all (assert the memory store's `search` was called, not
      `subprocess.run`), and its `agent_runs` row has `agent_name='engine'`.
    - SPEC's payload resolution: given a completed RESEARCH run, SPEC
      receives the research bundle; given a completed SPEC run, PLANNING
      and EXECUTION both receive it alongside their existing inputs.
    - Verify-command output appears in the reviewer's payload (mock
      `subprocess.run` for the verify command specifically, separate from
      the mock already used for the `claude`/`agy` CLI calls).
    - Wisdom write-back fires exactly once when a task reaches `completed`
      (both via REVIEW approval and via gate approval) and not at all on
      rejection or on a task that's still in progress.
    - All pre-existing tests in this file must still pass unmodified in
      behavior (some may need their setup extended to include a RESEARCH/
      SPEC row so `next_role()` reaches the stage the test actually
      targets — that's expected and fine; changing what a test *asserts*
      about existing PLANNING/EXECUTION/REVIEW behavior is not).

## Verification

1. `python verify_handoff.py` — full suite green, including every
   pre-existing test. This is the single most important check in this
   phase: `next_role()` and `_auto_resolve_payload()` are the load-bearing
   core of the entire autonomous pipeline, and a regression here breaks
   every task, old and new.
2. Do not touch `src/*.js`, `index.html`, or `src/styles.css` in this phase
   — the frontend still expects a 3-stage pipeline until Phase 3 lands, and
   that's fine; the new stages will simply not render yet (they'll still be
   correctly recorded in the database and visible in the Audit Trail's raw
   run list, which already handles arbitrary roles generically).

## Risk assessment

- **Level:** medium
- **Notes:** This changes core orchestration logic that the entire working
  pipeline depends on today. Run `python verify_handoff.py` after every
  sub-step in section "Implementation steps" above, not just once at the
  end — catching a regression in `next_role()` immediately after the change
  that caused it is far cheaper than finding it after also having changed
  five other things.
