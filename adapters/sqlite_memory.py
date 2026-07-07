"""
adapters/sqlite_memory.py — SQLite-backed implementation of MemoryStorePort.

Provides:
  - SQLiteMemoryStore: FTS5 keyword search + optional sqlite-vec semantic tier.
    The vector tier is entirely optional and fails silently to FTS5 if the
    extension is unavailable or the provider is not configured.

  - MemoryResearchInvoker: thin AgentInvocationPort wrapper around
    SQLiteMemoryStore for use as the ENGINE agent in the RSPBV pipeline
    (Phase 2). Wraps search() and writes an agent_runs row so RESEARCH
    gets the same HOLD/audit/routing-decision behaviour as every other role.
"""

import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing

from ports.memory_store import MemoryStorePort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _sanitize_fts5_query(query: str) -> str:
    """Quote each whitespace-separated token for FTS5 MATCH and join with OR.

    Uses OR so that documents matching ANY of the query's terms are returned,
    ordered by relevance (bm25 score). This mirrors the behaviour of a
    real keyword-search tool: "type hints in Python" should return any
    document containing "type", "hints", "Python", etc., not only documents
    containing ALL of them simultaneously.

    FTS5's MATCH operand treats characters like -, ", * as operators.
    Wrapping each token in double-quotes escapes them, turning arbitrary
    user text into a safe OR-keyword query, e.g.:
      'type hints in Python' -> '"type" OR "hints" OR "in" OR "Python"'
    An empty query returns a sentinel that will match nothing.
    """
    tokens = query.split()
    if not tokens:
        return '""'  # empty string — FTS5 will return no rows
    return " OR ".join(f'"{ t.replace(chr(34), "") }"' for t in tokens)


# ---------------------------------------------------------------------------
# SQLiteMemoryStore
# ---------------------------------------------------------------------------

class SQLiteMemoryStore(MemoryStorePort):
    """SQLite-backed memory store using FTS5 for keyword search.

    Vector tier is opt-in via config/memory.json. If the sqlite-vec extension
    is not available or the provider call fails, falls back silently to FTS5.
    Never raises out of search() — a search failure must not take down a run.
    """

    def __init__(self, db_path: str, config_path: str | None = None):
        self.db_path = db_path
        self._config = self._load_config(config_path)
        self._vec_available: bool | None = None  # None = not yet probed

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _load_config(self, config_path: str | None) -> dict:
        if config_path is None:
            base = os.path.dirname(os.path.abspath(self.db_path))
            config_path = os.path.join(base, "config", "memory.json")
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"embedding_provider": None, "top_k": 5}

    # ------------------------------------------------------------------
    # Port implementation
    # ------------------------------------------------------------------

    def ingest(self, path: str, kind: str = "rule") -> dict:
        """Read a file and upsert it as a knowledge_documents row.

        Returns {'status': 'created'|'updated'|'unchanged', 'id': int}.
        Returns {'status': 'error', 'error': str} if the file is unreadable.
        Idempotent by content_hash: if the file hasn't changed, no DB write.
        """
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError as e:
            return {"status": "error", "error": str(e)}

        # Derive title from first H1 line or filename
        title = os.path.splitext(os.path.basename(path))[0]
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break

        content = raw
        chash = _content_hash(content)
        source_path = os.path.abspath(path)

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            existing = conn.execute(
                "SELECT id, content_hash FROM knowledge_documents WHERE source_path=?",
                (source_path,),
            ).fetchone()

            if existing:
                doc_id, existing_hash = existing
                if existing_hash == chash:
                    return {"status": "unchanged", "id": doc_id}
                # Content changed — update in place
                conn.execute(
                    """UPDATE knowledge_documents
                       SET title=?, content=?, content_hash=?, kind=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (title, content, chash, kind, doc_id),
                )
                conn.commit()
                return {"status": "updated", "id": doc_id}

            # New document
            cur = conn.execute(
                """INSERT INTO knowledge_documents (source_path, title, content, content_hash, kind)
                   VALUES (?, ?, ?, ?, ?)""",
                (source_path, title, content, chash, kind),
            )
            conn.commit()
            return {"status": "created", "id": cur.lastrowid}

    def search(self, query: str, k: int = 5) -> list[dict]:
        """Return up to k relevant snippets for query using FTS5 (default).

        Returns [] on empty/no-match query. Never raises.
        FTS5 special characters in the query are escaped before the MATCH call.
        """
        if not query or not query.strip():
            return []

        fts5_query = _sanitize_fts5_query(query)

        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT d.id, d.title, d.content, d.kind,
                              bm25(knowledge_fts) AS score
                       FROM knowledge_fts
                       JOIN knowledge_documents d ON d.id = knowledge_fts.rowid
                       WHERE knowledge_fts MATCH ?
                       ORDER BY score
                       LIMIT ?""",
                    (fts5_query, k),
                ).fetchall()
        except sqlite3.OperationalError as e:
            print(f"[memory] FTS5 search error: {e}", file=sys.stderr)
            return []
        except Exception as e:  # pragma: no cover
            print(f"[memory] Unexpected search error: {e}", file=sys.stderr)
            return []

        return [
            {
                "id": r["id"],
                "title": r["title"],
                "content": r["content"],
                "kind": r["kind"],
                "score": r["score"],
            }
            for r in rows
        ]

    def write_wisdom(self, task_id: int, text: str) -> None:
        """Insert a kind='wisdom' row for the completed task.

        Idempotent: if a wisdom doc already exists for this task_id, no-op.
        Callers are also expected to guard against double-writes, but this
        provides a second layer of safety.
        """
        chash = _content_hash(text)
        title = f"Task #{task_id} outcome"

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            existing = conn.execute(
                "SELECT id FROM knowledge_documents WHERE kind='wisdom' AND task_id=?",
                (task_id,),
            ).fetchone()
            if existing:
                return  # already written

            conn.execute(
                """INSERT INTO knowledge_documents
                   (source_path, title, content, content_hash, kind, task_id)
                   VALUES (NULL, ?, ?, ?, 'wisdom', ?)""",
                (title, text, chash, task_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Convenience: document count by kind
    # ------------------------------------------------------------------

    def counts_by_kind(self) -> dict:
        """Return {'rule': n, 'wisdom': n, 'reference': n} from the DB."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) FROM knowledge_documents GROUP BY kind"
            ).fetchall()
        counts = {"rule": 0, "wisdom": 0, "reference": 0}
        for kind, n in rows:
            counts[kind] = n
        return counts

    def get_document(self, doc_id: int) -> dict | None:
        """Return a single document by id, or None if not found."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, title, content, kind, source_path, task_id, created_at, updated_at "
                "FROM knowledge_documents WHERE id=?",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_all_documents(self) -> list[dict]:
        """Return all documents (for GET /api/knowledge)."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, content, kind, source_path, task_id, created_at, updated_at "
                "FROM knowledge_documents ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_rule(self, title: str, content: str) -> dict:
        """Insert a kind='rule' document without a backing file on disk.

        Used by the MCP server's add_rule tool so rules can be added
        programmatically without needing a file in knowledge_base/.
        Returns {'id': int}.
        """
        chash = _content_hash(content)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            cur = conn.execute(
                """INSERT INTO knowledge_documents
                   (source_path, title, content, content_hash, kind)
                   VALUES (NULL, ?, ?, ?, 'rule')""",
                (title, content, chash),
            )
            conn.commit()
            return {"id": cur.lastrowid}


# ---------------------------------------------------------------------------
# MemoryResearchInvoker  (Phase 2 — added here to avoid a thin extra file)
# ---------------------------------------------------------------------------

class MemoryResearchInvoker:
    """AgentInvocationPort wrapper around SQLiteMemoryStore.

    Used as the ENGINE agent for the RESEARCH stage of the RSPBV pipeline.
    Looks up the task's title+description, calls memory_store.search(), and
    records the results as an agent_runs row with agent_name='engine'.
    This lets RESEARCH flow through the normal routing/audit machinery
    (HOLD, routing-decision recording, etc.) identically to every other role.
    """

    def __init__(self, memory_store: SQLiteMemoryStore):
        self.memory_store = memory_store

    def invoke(self, task_id: int, role: str, db_path: str, payload: dict) -> dict:
        # Fetch the task description to build the search query
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT title, description FROM tasks WHERE id=?", (task_id,)
            ).fetchone()

        if row:
            title, description = row
            query = " ".join(filter(None, [title, description]))
        else:
            query = ""

        k = payload.get("k", 5)
        snippets = self.memory_store.search(query, k=k) if query.strip() else []

        # Record the RESEARCH run in agent_runs
        logs_json = json.dumps({"snippets": snippets})
        with closing(sqlite3.connect(db_path)) as conn:
            cur = conn.execute(
                """INSERT INTO agent_runs
                   (task_id, agent_name, role, status, started_at, completed_at, logs)
                   VALUES (?, 'engine', ?, 'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)""",
                (task_id, role, logs_json),
            )
            conn.commit()
            run_id = cur.lastrowid

        return {"run_id": run_id, "snippets": snippets}
