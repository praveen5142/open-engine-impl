# Phase 1 — Embedded Open-Brain (memory layer)

```json
{
  "work_order_id": "OE-V2-PHASE1-OPEN-BRAIN",
  "task_summary": "Add a local-first memory layer to Open Engine: a knowledge_base/ folder of plain-text rules, indexed into the existing db.sqlite via SQLite FTS5 keyword search by default, with an optional sqlite-vec semantic-search tier that activates only if an embedding provider is configured. No new services, no new required dependencies, no separate database file.",
  "recommended_action": "proceed"
}
```

## Context (read this before writing any code)

Open Engine is a stdlib-only Python backend (`server.py`) + vanilla-JS
frontend running a CLI-adapter pipeline (`claude` for planning/review, `agy`
for execution). This phase adds nothing to that pipeline yet — it just
builds the memory store as a standalone, tested layer. Phase 2 wires it in.

Read these existing files fully before starting, so new code matches
established patterns instead of inventing new ones:

- `db_schema.sql` — table definitions, existing CHECK constraints.
- `server.py::_migrate_schema` (and `_column_exists`) — the idempotent,
  additive migration pattern already used for every schema change in this
  project. New tables/columns must follow this exact pattern, including
  whatever safety allowlisting (`SAFE_TABLES`/`SAFE_COLUMNS`) is currently
  present in that function — check its current state, it may have evolved.
- `ports/agent_invocation.py`, `ports/capability_store.py` — the existing
  Port (ABC) style: small, single-purpose interfaces.
- `adapters/sqlite_capability_store.py` — an existing SQLite-backed adapter
  implementing a port, for the connection/query style to match.
- `verify_handoff.py` — test style (stdlib `unittest`, temp SQLite DB per
  test via `make_temp_db()`/`remove_db()` helpers already defined at the top
  of the file).

## Implementation steps

1. **Schema** — add to `db_schema.sql`:
   ```sql
   CREATE TABLE IF NOT EXISTS knowledge_documents (
     id           INTEGER PRIMARY KEY AUTOINCREMENT,
     source_path  TEXT,
     title        TEXT NOT NULL,
     content      TEXT NOT NULL,
     content_hash TEXT NOT NULL,
     kind         TEXT CHECK(kind IN ('rule','wisdom','reference')) NOT NULL,
     task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
     created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
     updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   );

   CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
     title, content, content='knowledge_documents', content_rowid='id'
   );

   CREATE TRIGGER IF NOT EXISTS knowledge_documents_ai AFTER INSERT ON knowledge_documents BEGIN
     INSERT INTO knowledge_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
   END;
   CREATE TRIGGER IF NOT EXISTS knowledge_documents_ad AFTER DELETE ON knowledge_documents BEGIN
     INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content);
   END;
   CREATE TRIGGER IF NOT EXISTS knowledge_documents_au AFTER UPDATE ON knowledge_documents BEGIN
     INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content);
     INSERT INTO knowledge_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
   END;

   CREATE TABLE IF NOT EXISTS knowledge_vectors (
     doc_id      INTEGER REFERENCES knowledge_documents(id) ON DELETE CASCADE,
     chunk_index INTEGER NOT NULL,
     chunk_text  TEXT NOT NULL,
     embedding   BLOB,
     PRIMARY KEY (doc_id, chunk_index)
   );
   ```
   These are FTS5 "external content" tables — the triggers keep `knowledge_fts`
   in sync with `knowledge_documents`; without them the index silently goes
   stale. `knowledge_vectors` is created regardless of whether the vector
   tier is ever used — it's just an empty table if not.

2. **Migrate existing live databases** — `CREATE TABLE`/`CREATE TRIGGER IF
   NOT EXISTS` statements are safe to re-run unconditionally (unlike `ALTER
   TABLE`, which needs the existing `_column_exists` guard). Add the exact
   same statements from step 1 into `server.py::_migrate_schema`, executed
   unconditionally after the existing column-migration logic, so a database
   created before this phase gets the new tables on next server start.

3. **`knowledge_base/README.md`** at repo root — explain the format: plain
   `.md`/`.txt` files, one rule or topic per file, first `# H1` line (or the
   filename if none) becomes the document title, reindex via the dashboard
   button or `POST /api/knowledge/reindex`. This folder is the "rule" input
   surface — files a human writes by hand.

4. **`ports/memory_store.py`** — new port:
   ```python
   from abc import ABC, abstractmethod

   class MemoryStorePort(ABC):
       @abstractmethod
       def ingest(self, path: str) -> dict:
           """Read a file, upsert it as a knowledge_documents row. Returns
           {'status': 'created'|'updated'|'unchanged', 'id': int}."""

       @abstractmethod
       def search(self, query: str, k: int = 5) -> list[dict]:
           """Return up to k {id, title, content, kind, score} snippets
           relevant to query. Never raises for an empty/no-match query -
           returns []."""

       @abstractmethod
       def write_wisdom(self, task_id: int, text: str) -> None:
           """Record a completed task's outcome as a kind='wisdom' document
           so future searches can retrieve it."""
   ```

5. **`adapters/sqlite_memory.py`** — `SQLiteMemoryStore(MemoryStorePort)`:
   - `ingest(path)`: read the file; `title` = first line starting with `# `
     stripped of the marker, else the filename without extension; `kind` =
     `'rule'` for anything ingested from `knowledge_base/` (pass `kind` as a
     parameter with that default); `content_hash` = `hashlib.sha256(content.encode()).hexdigest()`.
     Look up any existing row by `source_path`; if `content_hash` matches,
     do nothing and return `{'status': 'unchanged', ...}` (this is what
     makes reindexing idempotent — re-running it after no changes must not
     create duplicate rows or bump `updated_at`). If it differs or no row
     exists, insert or update and return `'created'`/`'updated'` accordingly.
   - `search(query, k)`: default tier is FTS5:
     ```python
     cur.execute(
         "SELECT d.id, d.title, d.content, d.kind, bm25(knowledge_fts) AS score "
         "FROM knowledge_fts JOIN knowledge_documents d ON d.id = knowledge_fts.rowid "
         "WHERE knowledge_fts MATCH ? ORDER BY score LIMIT ?",
         (fts5_query, k)
     )
     ```
     FTS5's `MATCH` operand has its own query syntax (operators like `-`,
     `"`, `*` are meaningful) - a raw user/task-description string can
     contain characters that make `MATCH` raise `sqlite3.OperationalError`.
     Sanitize by quoting each whitespace-split token individually and
     joining with spaces (implicit AND), e.g. turn `explain the repo` into
     `"explain" "the" "repo"`, so arbitrary input can't break FTS5 syntax.
     Catch `sqlite3.OperationalError` around the query anyway and return
     `[]` rather than propagating - a memory-search failure must never take
     down a pipeline run.
   - Optional vector tier: read `config/memory.json`. If
     `embedding_provider` is non-null, try `conn.enable_load_extension(True)`
     then `conn.load_extension("vec0")` (the sqlite-vec extension name) in a
     `try/except`; if either the config is absent, the extension fails to
     load, or the configured provider's HTTP call fails, silently fall back
     to the FTS5 result - never raise out of `search()`. If the vector tier
     genuinely is available, embed the query via the configured provider
     (see `config/memory.json` shape below) and query `knowledge_vectors`
     for nearest neighbors, then return those in place of (or merged with,
     your call, but document which) the FTS5 results.
   - `write_wisdom(task_id, text)`: insert into `knowledge_documents` with
     `kind='wisdom'`, `title=f"Task #{task_id} outcome"`, `source_path=None`,
     `task_id=task_id`, computing `content_hash` the same way.

6. **`config/memory.json`** — new file:
   ```json
   {
     "embedding_provider": null,
     "top_k": 5
   }
   ```
   When non-null, `embedding_provider` should look like
   `{"kind": "gemini", "model": "...", "endpoint": "...", "api_key_env": "..."}`
   or `{"kind": "openai_compatible", ...}` — implement whichever one shape
   you actually wire up first; leave the other as a documented TODO comment
   rather than half-implementing both. Read the API key from the named
   environment variable, never hardcode one or store one in this file.

7. **`server.py` endpoints** — add three, following the existing handler +
   route-constant pattern already in this file (see `ROUTE_ARTIFACTS_TASK_ID`
   etc. for the style):
   - `POST /api/knowledge/reindex` — walk `knowledge_base/` for `*.md`/`*.txt`
     files, call `store.ingest(path)` for each, return
     `{"created": n, "updated": n, "unchanged": n}`.
   - `GET /api/knowledge` — return `{"by_kind": {"rule": n, "wisdom": n, "reference": n}, "documents": [...]}`.
   - `GET /api/knowledge/search?q=...` — call `store.search(q)`, return the
     list as JSON. This exists for debugging/future UI use, not required by
     any other part of this phase.

8. **Tests** — add a `TestMemoryStore` class to `verify_handoff.py` using
   the existing `make_temp_db()`/`remove_db()` helpers:
   - Ingesting a file then searching for a word in it returns that document.
   - Ingesting the same content twice (same `content_hash`) is a no-op the
     second time (`status == 'unchanged'`, no duplicate row).
   - Ingesting changed content at the same `source_path` updates the
     existing row rather than creating a second one.
   - `search()` with a query matching nothing returns `[]`, not an error.
   - A query containing FTS5-special characters (e.g. `explain the "repo"`
     or a string with a leading `-`) does not raise.
   - `write_wisdom()` creates exactly one row with `kind='wisdom'` and the
     right `task_id`.

## Verification

1. `python verify_handoff.py` — all existing tests plus the new
   `TestMemoryStore` tests must pass.
2. Manually confirm whether this machine's Python 3.12 `sqlite3` module
   permits `enable_load_extension` at all (some stdlib builds disable it) -
   this doesn't block anything in this phase since the vector tier is
   optional, but note the result in your summary for whoever reads it next.
3. Do not touch `application/orchestration_service.py`, `domain/agent.py`,
   `config/routing.json`, or any adapter's `invoke()` method in this phase -
   wiring the memory store into the pipeline is Phase 2's job, not this
   one's. If Phase 1 is done correctly, `SQLiteMemoryStore` and
   `MemoryStorePort` exist and are tested, but nothing in the existing
   pipeline calls them yet.

## Risk assessment

- **Level:** low
- **Notes:** Purely additive — new tables, new files, new endpoints, zero
  changes to existing tables or existing endpoint behavior. FTS5 ships in
  Python's stdlib `sqlite3` (available since Python 3.x builds with SQLite
  compiled with FTS5, which is the default on the platforms this project
  targets) - no new pip dependency for the default tier. The vector tier
  must be entirely best-effort: any failure to load the extension or reach
  an embedding provider degrades to FTS5-only silently, never raises, and
  never blocks `python verify_handoff.py` from passing on a machine with no
  provider configured (the default state).
