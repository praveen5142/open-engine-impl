# Open Engine v2 playbook — Embedded Open-Brain + RSPBV

Four self-contained work orders that take Open Engine from its current
3-stage pipeline (PLANNING → EXECUTION → REVIEW) to the 5-stage RSPBV
pipeline (Research → Spec → Plan → Build → Verify) with an embedded,
local-first memory layer ("open-brain": SQLite FTS5 by default, optional
`sqlite-vec` semantic search — no PostgreSQL, no Obsidian, no background
services).

**Final intent of this application** (do not lose sight of this while
implementing any phase): an autonomous implementation pipeline built on the
`claude` and `antigravity` CLI adapters. Memory/RSPBV is an evolution of that
pipeline, not a replacement for the CLI-adapter architecture.

## How to use these

Each `phase-N-*.md` file is a complete work order in the same shape the
app's own PLANNING stage produces (`work_order_id`, `task_summary`,
`implementation_steps`, `risk_assessment`, `recommended_action`) so it can be
handed directly to Antigravity (or pasted as a task description into Open
Engine itself and run through the dashboard) without needing this
conversation's context.

**Run them in order. Do not skip ahead.** Each phase's acceptance criteria
are a prerequisite for the next:

1. [phase-1-embedded-open-brain.md](phase-1-embedded-open-brain.md) — the memory layer itself (schema, port, adapter, endpoints).
2. [phase-2-rspbv-pipeline.md](phase-2-rspbv-pipeline.md) — wires memory into the orchestrator as two new pipeline stages.
3. [phase-3-dashboard-evolution.md](phase-3-dashboard-evolution.md) — frontend catches up to the 5-stage pipeline.
4. [phase-4-mcp-server.md](phase-4-mcp-server.md) — optional, deferred: exposes the same brain over MCP to other clients.

After each phase:
1. Run `python verify_handoff.py` — must be fully green before moving on.
2. Do the manual verification listed at the end of that phase's file.
3. Commit and push before starting the next phase's work order.
4. **Stop.** Do not continue into the next phase's file in the same run unless explicitly told to — each phase should be reviewed before the next one starts, since each one changes behavior the previous phase's tests already locked in.

## Non-negotiables that apply to every phase

- **Keep the CLI adapters.** Do not replace `claude` CLI invocations with a direct HTTP API call to Anthropic. This was deliberately decided against — see phase 2's context section for why.
- **Stdlib-only, no new required pip dependencies.** FTS5 ships in Python's own `sqlite3`. `sqlite-vec` and any embedding provider must be optional and fail gracefully to keyword search if unavailable — never a hard dependency, never a crash.
- **Reuse existing patterns, don't invent parallel ones.** This codebase already has established conventions for: additive schema migration (`server.py::_migrate_schema`), CHECK-constraint table rebuilds (same function, agent_runs.status precedent), role-based orchestration (`domain/agent.py`, `application/orchestration_service.py::next_role`/`_auto_resolve_payload`), and disclosure-panel UI (`.review-toggle` pattern in `src/ui.js`). Read the relevant existing code before writing new code in the same area, and match its shape.
- **Never regress the existing pipeline.** PLANNING → EXECUTION → REVIEW works today and is covered by `verify_handoff.py`. Every phase must keep all existing tests passing, not just add new ones.
