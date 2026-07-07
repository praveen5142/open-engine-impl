# Phase 3 — Dashboard evolution (5-stage UI + Knowledge panel)

```json
{
  "work_order_id": "OE-V2-PHASE3-DASHBOARD",
  "task_summary": "Update the frontend to visualize the 5-stage RSPBV pipeline (Research/Spec/Plan/Build/Verify) and expose the Phase 1 memory layer (knowledge document counts, reindex button, research-context and spec panels), without breaking any existing rendering behavior for historical 3-stage tasks.",
  "recommended_action": "proceed"
}
```

## Prerequisite

**Phase 2 must be complete, tested, and merged first.** This phase renders
data that Phase 2's backend produces (RESEARCH/SPEC `agent_runs` rows,
`GET /api/knowledge`). Do not start it against a backend that doesn't
produce that data yet.

## Context (read this before writing any code)

This is a frontend-only phase (plus two small, purely additive backend
fields — see step 6). Do not touch `application/orchestration_service.py`,
`domain/agent.py`, `config/routing.json`, or any adapter's `invoke()`
method in this phase — that was Phase 2's job.

Read these existing files in full before changing anything, and match their
established patterns rather than inventing new ones:
- `src/ui.js` — specifically `renderFocusMode()` (the stepper + status
  banner) and `renderAtomicReview()` (Approval Gates / Work Order /
  Artifacts / Audit Trail panels, all built on a shared `.review-toggle`
  disclosure pattern).
- `src/state.js` — the state shape (`agentRuns`, `artifacts`, etc., all
  keyed by `taskId`) and the plain pub/sub `set`/`subscribe` mechanism.
- `src/api.js` — the `load*`/action-function pairing convention and the
  `SSE_HANDLERS` object for live updates.
- `index.html` — current Frame 1/2/3 markup structure.

## Implementation steps

1. **Stepper (5 dots)** — in `renderFocusMode()`, alongside the existing
   `planRun`/`execRun`/`reviewRun` lookups (`runByRole('PLANNING')` etc.),
   add `researchRun = runByRole('RESEARCH')` and `specRun = runByRole('SPEC')`.
   Extend the stepper's HTML to render 5 `.step` blocks in order: Research
   (engine) → Spec (Claude) → Plan (Claude) → Build (Antigravity) → Review
   (Claude), reusing the exact same `stepClass()` function and `.step`/
   `.step-dot` CSS classes already used for the other three — do not invent
   new dot styles. Confirm `stepClass(undefined)` still returns `''` so a
   historical task that only ever had PLANNING/EXECUTION/REVIEW rows (no
   RESEARCH/SPEC) renders those two new dots as simply not-yet-run, not
   broken or throwing.

2. **Research Context panel** — in `renderAtomicReview()`, add a new
   disclosure section (same `.review-toggle` expand/collapse markup already
   used for the Artifacts cards) showing the RESEARCH run's retrieved
   snippets: title + a truncated content preview per snippet, pulled from
   that run's `logs` JSON. If no RESEARCH run exists yet for the active
   task, render the same "not yet available" placeholder style already used
   by the Work Order panel's empty state.

3. **Spec panel** — extend the existing "Work Order" panel (or add a
   clearly-labeled sub-section within it, your call — avoid adding a whole
   third sidebar section for this, two clearly labeled sub-blocks in one
   panel is enough) to show the SPEC run's `logs` (objective, acceptance
   criteria, out-of-scope, files-expected) above the existing PLANNING work
   order display, using the same `syntaxHighlight()`/`json-viewer` rendering
   already used for the work order JSON.

4. **Knowledge panel** (Command Center / Frame 1) — new small section:
   - Doc counts by kind, from `GET /api/knowledge`.
   - A "Reindex knowledge_base" button wired to `POST /api/knowledge/reindex`,
     following the exact button+handler wiring pattern already used for
     "Run Probe" in this same frame (disable while in flight, push a
     `State.pushLog(...)` line on completion, same as `runProbe()` in
     `src/api.js` already does).
   - Add `loadKnowledge()`/`reindexKnowledge()` functions to `src/api.js`
     mirroring the shape of `loadProbe()`/`runProbe()`.

5. **Memory tier indicator** — surface whether the vector tier is active
   (from Phase 1's capability check) somewhere in the existing capability
   probe display, as a small badge/row alongside the `claude`/`antigravity`/
   `codex` rows already shown there. If Phase 1 didn't expose this via
   `GET /api/probe`, add a minimal field for it there rather than a new
   endpoint — keep this lightweight, it's informational only.

6. **Verify command input** — add an optional "Verify command" text input
   to the new-task form (`index.html`), threaded through `createTask()` in
   `src/api.js` and the task-creation handler in `server.py` (`_create_task`)
   to persist into the `tasks.verify_command` column Phase 2 added. Check
   `_create_task`'s current body-parsing style before extending it, so the
   new optional field is handled the same way the existing optional
   `description` field already is.

7. **Manual browser verification** (do this yourself, don't just eyeball the
   code):
   - Start the server, open the dashboard.
   - Confirm the 5-dot stepper renders on a *new* task with none of the
     stages run yet (all 5 dots in the "not started" state).
   - Add a rule file to `knowledge_base/`, reindex via the new button,
     confirm the Knowledge panel's counts update.
   - Create a task, run it, confirm the Research Context panel populates
     with something plausibly related to the rule you just added, and the
     Spec panel shows real acceptance criteria before the Plan/Build/Review
     stages run.
   - Open an *old* task (created before Phase 2/3 existed, with only
     PLANNING/EXECUTION/REVIEW rows) and confirm it still renders correctly
     — Research/Spec dots show as not-run, nothing errors in the browser
     console.

8. Run `python verify_handoff.py` (no backend logic changed in this phase
   beyond the two additive fields in step 4/6, but confirm nothing broke
   regardless) — then **stop** before Phase 4.

## Verification

1. `python verify_handoff.py` green.
2. The manual browser pass in step 7 above, actually performed, not assumed.
3. Browser console has zero new errors on both a brand-new task and a
   historical pre-Phase-2 task.

## Risk assessment

- **Level:** low
- **Notes:** Frontend-only plus two small additive backend fields
  (`GET /api/knowledge` consumption, `tasks.verify_command` persistence).
  The main risk is regressing the rendering of historical tasks that
  predate RESEARCH/SPEC rows — explicitly test against one, don't just test
  against freshly-created tasks where every stage is present.
