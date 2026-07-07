# Phase 4 — MCP server over the brain (optional, deferred)

```json
{
  "work_order_id": "OE-V2-PHASE4-MCP-SERVER",
  "task_summary": "Expose the same open-brain knowledge store to any MCP-speaking client (e.g. a Claude Code or Claude Desktop session) via a small stdlib-only MCP server, so the memory built in Phase 1 isn't locked inside Open Engine's own dashboard.",
  "recommended_action": "hold_until_prior_phases_proven"
}
```

## Prerequisite

**Phases 1-3 complete, merged, and actually used for a handful of real
tasks first.** This phase is explicitly optional and last on purpose:
nothing in Phases 1-3 depends on it, and an MCP server's tool surface is the
piece most likely to need reshaping once you've actually seen what's useful
to query from outside the dashboard. Do not start this phase speculatively —
start it once you've noticed yourself wanting to query the brain from
somewhere other than the Open Engine UI.

## Context

Consistent with this project's zero-dependency philosophy (stdlib-only
backend, no pip installs required to run the core app), this MCP server
should be hand-rolled against the MCP JSON-RPC-over-stdio protocol using
only stdlib (`json`, `sys`, `threading`), not an MCP SDK dependency. It must
operate on the **same `db.sqlite`** the dashboard uses — this is a second
window into the same brain, not a separate one.

Before writing any code:
- Confirm the exact current MCP stdio transport framing (newline-delimited
  JSON-RPC vs. `Content-Length`-prefixed framing) against up-to-date MCP
  protocol reference material available to you at implementation time —
  don't guess or rely on stale training data for the wire format, since
  getting the framing wrong means a client can't talk to this at all. If you
  can't find authoritative current reference material, implement the
  simpler newline-delimited variant, clearly document that choice and the
  uncertainty in your summary, and flag it for a human to confirm against
  a real client before relying on it.
- Read `adapters/sqlite_memory.py` and `ports/memory_store.py` (from
  Phase 1) in full — this phase is a thin transport wrapper around that
  existing, already-tested class. It should not reimplement any storage or
  search logic.

## Implementation steps

1. New `mcp_server.py` at repo root: a loop reading JSON-RPC 2.0 requests
   from stdin, dispatching `initialize`, `tools/list`, and `tools/call` to
   handler functions, writing JSON-RPC responses to stdout. Resolve the
   database path the same way `server.py` does (`DB_PATH` next to this
   file), so it's guaranteed to be the same file the dashboard reads/writes.

2. Implement exactly three tools, each a thin wrapper over
   `SQLiteMemoryStore` (import and instantiate it directly — do not go
   through an HTTP call to the running dashboard server, this should work
   even if the dashboard isn't running):
   - `search_knowledge(query: str, k: int = 5)` → calls `.search()`.
   - `get_document(id: int)` → direct row lookup by id.
   - `add_rule(title: str, content: str)` → inserts a `kind='rule'` document
     (equivalent to dropping a file in `knowledge_base/` and reindexing, but
     without needing a file on disk).

3. Document registration in `README.md`: a short new section with the JSON
   config snippet for adding this as an MCP server to Claude Code/Desktop's
   `mcpServers` config — `command: "python"`, `args: ["mcp_server.py"]`,
   correct `cwd` pointing at this repo.

4. **Verify with a real client if one is available in your environment.**
   Register the server, call `search_knowledge` for something you know is
   in `knowledge_base/`, confirm real results come back. If no MCP client is
   available to test against in this environment, say so explicitly in
   your summary rather than claiming this was verified — this is the one
   part of the whole playbook most likely to have a protocol-level bug that
   only shows up against a real client.

5. Run `python verify_handoff.py` — confirm zero regressions (this phase
   adds one standalone file; it should not be able to break anything else,
   and if it does, that itself indicates a mistake in this phase, e.g.
   accidentally importing something with a side effect on module load).

## Verification

1. `python verify_handoff.py` still green.
2. Either a real successful round-trip through an actual MCP client, or an
   explicit, honest note that this specific step could not be self-verified
   in this environment and needs a human to confirm it.

## Risk assessment

- **Level:** low for the codebase (additive, standalone file, zero changes
  to `server.py`'s existing request path or any other existing file).
  **Medium for correctness** specifically because the MCP wire protocol is
  hand-rolled here rather than using an SDK — be conservative, and do not
  report this phase as fully done without a real client test.
