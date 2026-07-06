"""
server.py  -  Open Engine Handoff MVP Local Backend
Python stdlib only: http.server, sqlite3, subprocess, json, uuid, threading

Usage:
    python server.py [port]   (default port: 8000)

Endpoints:
    GET  /                          -> serve index.html
    GET  /src/*                     -> serve src/ static files
    GET  /api/tasks                 -> list tasks
    POST /api/tasks                 -> create task
    GET  /api/tasks/<id>            -> get task + agent_runs
    POST /api/telemetry             -> append telemetry event
    GET  /api/events                -> SSE stream
    POST /api/approval              -> approve/reject gate
    GET  /api/approval              -> list pending gates
    POST /api/artifacts             -> ingest return artifact
    GET  /api/artifacts/<task_id>   -> list artifacts
    POST /api/artifacts/<id>/open   -> export artifact content to a file and open
                                          Windows' native "Open with" app picker
    POST /api/probe                 -> run Phase 0 capability probe
    GET  /api/probe                 -> last probe results
    GET  /api/run/<task_id>/poll    -> poll Antigravity return artifacts
    POST /api/tasks/<task_id>/delegate -> the dashboard's only "run a task" action.
                                          Chains PLANNING (Claude) -> EXECUTION
                                          (Antigravity) -> REVIEW (Claude) to
                                          completion automatically, looping back
                                          to EXECUTION if REVIEW requests changes
                                          (capped retries), stopping on HOLD.
    GET  /api/project               -> currently selected project folder (or null)
    POST /api/project               -> select a project folder: {path, name?}
    GET  /api/fs/list?path=<path>   -> in-app folder browser (folders only); omit path
                                          for drive/root list
    POST /api/capability/reset      -> clear a stuck EXHAUSTED/cooldown state: {agent}
"""

import contextlib
import http.server
import json
import os
import re
import sqlite3
import string
import subprocess
import sys
import threading
import time
from io import BytesIO
from urllib.parse import urlparse, parse_qs

# Windows cp1252 safe output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# -- paths --------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "db.sqlite")
SRC_DIR  = os.path.join(BASE_DIR, "src")

# -- SSE subscriber registry ---------------------------------------------------
_sse_lock        = threading.Lock()
_sse_subscribers: list = []   # list of queue.Queue
SSE_HEARTBEAT_TIMEOUT = 15
SSE_QUEUE_MAXSIZE = 64


def _broadcast(event_type: str, data: dict):
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()
    with _sse_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for d in dead:
            _sse_subscribers.remove(d)


# -- DB helpers -----------------------------------------------------------------

@contextlib.contextmanager
def get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _column_exists(conn, table, column) -> bool:
    SAFE_TABLES = {"tasks", "capability_probe", "agent_runs"}
    SAFE_COLUMNS = {
        "project_path",
        "quota_status",
        "quota_confidence",
        "quota_evidence",
        "cooldown_until",
        "role",
    }
    if table not in SAFE_TABLES:
        raise ValueError(f"Unsafe table name: {table}")
    if column not in SAFE_COLUMNS:
        raise ValueError(f"Unsafe column name: {column}")
    return column in [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate_schema(conn):
    """
    `CREATE TABLE IF NOT EXISTS` (used throughout db_schema.sql) only creates
    tables that don't exist yet - it is a silent no-op for tables that already
    exist, so it never adds new columns to a real, already-populated
    db.sqlite. Discovered by actually booting the server against this
    project's real db.sqlite during verification: every new column added by
    this round of work (tasks.project_path, capability_probe.quota_status/
    quota_confidence/quota_evidence/cooldown_until) and the widened
    agent_runs.status CHECK constraint (added 'quota_exceeded' and
    'skipped_degraded') were silently missing from the live database, so the
    very first delegate/probe/degraded-planning call would have crashed with
    'no such column' or a CHECK constraint failure. This migration is
    idempotent - safe to run on every startup.
    """
    # 1) New columns on existing tables (ALTER TABLE ADD COLUMN is additive
    #    and safe; SQLite has no "ADD COLUMN IF NOT EXISTS", so check first).
    column_additions = [
        ("tasks", "project_path", "TEXT"),
        ("capability_probe", "quota_status", "TEXT DEFAULT 'unknown'"),
        ("capability_probe", "quota_confidence", "REAL DEFAULT 1.0"),
        ("capability_probe", "quota_evidence", "TEXT"),
        ("capability_probe", "cooldown_until", "TIMESTAMP"),
        ("agent_runs", "role", "TEXT"),
    ]
    SAFE_TABLES = {"tasks", "capability_probe", "agent_runs"}
    SAFE_COLUMNS = {
        "project_path",
        "quota_status",
        "quota_confidence",
        "quota_evidence",
        "cooldown_until",
        "role",
    }
    SAFE_DECLARATIONS = {
        "TEXT",
        "TEXT DEFAULT 'unknown'",
        "REAL DEFAULT 1.0",
        "TIMESTAMP",
    }
    for table, column, decl in column_additions:
        if table not in SAFE_TABLES:
            raise ValueError(f"Unsafe table name: {table}")
        if column not in SAFE_COLUMNS:
            raise ValueError(f"Unsafe column name: {column}")
        if decl not in SAFE_DECLARATIONS:
            raise ValueError(f"Unsafe column declaration: {decl}")
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # 2) agent_runs.status CHECK constraint needs new allowed values
    #    ('quota_exceeded', 'skipped_degraded'). SQLite can't ALTER a CHECK
    #    constraint in place, so rebuild the table: rename, recreate with the
    #    current schema, copy rows across, drop the old one. Old rows only
    #    ever used a subset of the new allowed values, so the copy can't
    #    violate the new (wider) constraint.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_runs'"
    ).fetchone()
    if row and row[0] and "skipped_degraded" not in row[0]:
        conn.execute("PRAGMA foreign_keys=OFF")
        # IMPORTANT: by default SQLite's `ALTER TABLE ... RENAME TO` rewrites
        # *other* tables' REFERENCES clauses to follow the rename - so
        # renaming agent_runs -> agent_runs_old silently corrupts
        # approval_gates.agent_run_id into "REFERENCES agent_runs_old(id)".
        # After agent_runs_old is dropped, that FK points at a table that no
        # longer exists, and any later DELETE that touches agent_runs fails
        # with "no such table: agent_runs_old" (found by testing this exact
        # migration against the real project database). legacy_alter_table
        # disables that rewrite so approval_gates keeps saying "agent_runs",
        # which is valid again as soon as the new agent_runs table exists.
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("ALTER TABLE agent_runs RENAME TO agent_runs_old")
        conn.execute("""
            CREATE TABLE agent_runs (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id      INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
              agent_name   TEXT CHECK(agent_name IN ('codex','claude','antigravity')),
              status       TEXT CHECK(status IN ('pending','running','completed','failed','blocked','quota_exceeded','skipped_degraded')) DEFAULT 'pending',
              started_at   TIMESTAMP,
              completed_at TIMESTAMP,
              logs         TEXT
            )
        """)
        conn.execute("""
            INSERT INTO agent_runs (id, task_id, agent_name, status, started_at, completed_at, logs)
            SELECT id, task_id, agent_name, status, started_at, completed_at, logs FROM agent_runs_old
        """)
        conn.execute("DROP TABLE agent_runs_old")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")

    # Rebuild approval_gates if either: (a) fallout from an earlier run of
    # this migration left agent_run_id's FK dangling at "agent_runs_old"
    # (before the legacy_alter_table fix above existed), or (b) it predates
    # the 'review_decision' action_type (added so an exhausted REVIEW-retry
    # loop has something a human can actually act on instead of just sitting
    # blocked with no control anywhere in the UI).
    ag_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='approval_gates'"
    ).fetchone()
    if ag_row and ag_row[0] and ("agent_runs_old" in ag_row[0] or "review_decision" not in ag_row[0]):
        conn.execute("ALTER TABLE approval_gates RENAME TO approval_gates_old")
        conn.execute("""
            CREATE TABLE approval_gates (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
              agent_run_id  INTEGER REFERENCES agent_runs(id),
              action_type   TEXT CHECK(action_type IN ('write_file','execute_command','review_decision')),
              payload       TEXT NOT NULL,
              status        TEXT CHECK(status IN ('pending','approved','rejected')) DEFAULT 'pending',
              created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              reviewed_at   TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO approval_gates (id, task_id, agent_run_id, action_type, payload, status, created_at, reviewed_at)
            SELECT id, task_id, agent_run_id, action_type, payload, status, created_at, reviewed_at FROM approval_gates_old
        """)
        conn.execute("DROP TABLE approval_gates_old")

    conn.commit()


def init_db():
    schema = os.path.join(BASE_DIR, "db_schema.sql")
    with get_db_conn() as conn:
        conn.executescript(open(schema).read())
        _migrate_schema(conn)


def rows_as_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# -- Safety helpers ---------------------------------------------------------

ALLOWED_WRITE_PREFIX = os.path.abspath(os.path.join(BASE_DIR, "handoff-lab"))
ARTIFACT_EXPORT_DIR = os.path.abspath(os.path.join(BASE_DIR, "artifact_exports"))
SECRET_PATTERNS = [
    r"(?i)(api[_-]?key|secret|token|password|auth)[=:]\s*\S+",
    r"(?i)bearer\s+[A-Za-z0-9\-_\.]+",
    r"(?i)sk-[A-Za-z0-9]+",
    r"BEGIN\s+(RSA\s+)?PRIVATE\s+KEY",
]
_secret_re = [re.compile(p) for p in SECRET_PATTERNS]

ROUTE_TASK_ID = re.compile(r"^/api/tasks/(\d+)$")
ROUTE_ARTIFACTS_TASK_ID = re.compile(r"^/api/artifacts/(\d+)$")
ROUTE_RUN_POLL = re.compile(r"^/api/run/(\d+)/poll$")

ROUTE_ARTIFACT_OPEN = re.compile(r"^/api/artifacts/(\d+)/open$")
ROUTE_TASK_DELEGATE = re.compile(r"^/api/tasks/(\d+)/delegate$")


def _is_safe_path(path: str) -> bool:
    return os.path.abspath(path).startswith(ALLOWED_WRITE_PREFIX)


def _scrub_secrets(text: str) -> tuple[bool, str]:
    for pat in _secret_re:
        if pat.search(text):
            return True, "Payload rejected: secret-looking content detected"
    return False, ""


def _list_roots() -> list[dict]:
    """Top-level entries for the in-app folder browser (no OS file dialog
    is available since this is a plain stdlib HTTP server, not Electron)."""
    if os.name == "nt":
        drives = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append({"name": drive, "path": drive})
        return drives
    return [{"name": "/", "path": "/"}]


class HTTPError(Exception):
    pass


# -- HTTP handler ---------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [http] {self.address_string()} {fmt % args}")

    def _send_json(self, code: int, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, msg: str):
        self._send_json(code, {"error": msg})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON")
            raise HTTPError()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # -- static files -----------------------------------------------------------

    def _serve_file(self, path: str, mime: str):
        if not os.path.isfile(path):
            self._send_error(404, f"File not found: {path}")
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing ------------------------------------------------------------------

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            p = parsed.path.rstrip("/") or "/"

            if p == "/" or p == "/index.html":
                self._serve_file(os.path.join(BASE_DIR, "index.html"), "text/html")

            elif p.startswith("/src/"):
                rel = p[len("/src/"):]
                fpath = os.path.join(SRC_DIR, rel)
                ext = os.path.splitext(fpath)[1]
                mime_map = {".js": "application/javascript", ".css": "text/css",
                            ".json": "application/json", ".html": "text/html"}
                self._serve_file(fpath, mime_map.get(ext, "text/plain"))

            elif p == "/api/tasks":
                self._get_tasks(parse_qs(parsed.query))
            elif (m := ROUTE_TASK_ID.match(p)):
                tid = int(m.group(1))
                self._get_task(tid)
            elif p == "/api/events":
                self._sse_stream()
            elif p == "/api/approval":
                self._get_approvals()
            elif (m := ROUTE_ARTIFACTS_TASK_ID.match(p)):
                tid = int(m.group(1))
                self._get_artifacts(tid)
            elif p == "/api/probe":
                self._get_probe()
            elif (m := ROUTE_RUN_POLL.match(p)):
                tid = int(m.group(1))
                self._poll_antigravity(tid)
            elif p == "/api/project":
                self._get_project()
            elif p == "/api/fs/list":
                self._fs_list(parse_qs(parsed.query))
            else:
                self._send_error(404, "Not found")
        except HTTPError:
            pass

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            p = parsed.path.rstrip("/")

            if p == "/api/tasks":
                self._create_task()
            elif p == "/api/telemetry":
                self._append_telemetry()
            elif p == "/api/approval":
                self._post_approval()
            elif p == "/api/artifacts":
                self._post_artifact()
            elif (m := ROUTE_ARTIFACT_OPEN.match(p)):
                aid = int(m.group(1))
                self._open_artifact(aid)
            elif p == "/api/probe":
                self._run_probe()
            elif (m := ROUTE_TASK_DELEGATE.match(p)):
                tid = int(m.group(1))
                self._delegate(tid)
            elif p == "/api/project":
                self._set_project()
            elif p == "/api/capability/reset":
                self._reset_capability()
            else:
                self._send_error(404, "Not found")
        except HTTPError:
            pass

    # -- API handlers -------------------------------------------------------------

    def _get_tasks(self, query: dict | None = None):
        project = (query or {}).get("project", [None])[0]
        with get_db_conn() as db:
            if project:
                tasks = rows_as_dicts(db.execute(
                    "SELECT t.*, COUNT(ar.id) as run_count FROM tasks t "
                    "LEFT JOIN agent_runs ar ON ar.task_id=t.id WHERE t.project_path=? "
                    "GROUP BY t.id ORDER BY t.id DESC",
                    (project,)
                ).fetchall())
            else:
                tasks = rows_as_dicts(db.execute(
                    "SELECT t.*, COUNT(ar.id) as run_count FROM tasks t "
                    "LEFT JOIN agent_runs ar ON ar.task_id=t.id GROUP BY t.id ORDER BY t.id DESC"
                ).fetchall())
        self._send_json(200, tasks)

    def _get_task(self, task_id: int):
        with get_db_conn() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return self._send_error(404, "Task not found")
            runs = rows_as_dicts(db.execute(
                "SELECT * FROM agent_runs WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall())
        result = dict(task)
        result["agent_runs"] = runs
        self._send_json(200, result)

    def _create_task(self):
        body = self._read_json()
        title = body.get("title", "").strip()
        if not title:
            return self._send_error(400, "title is required")
        if len(title) > 200:
            return self._send_error(400, "title must be 200 characters or less")
        is_secret, msg = _scrub_secrets(json.dumps(body))
        if is_secret:
            return self._send_error(400, msg)
        with get_db_conn() as db:
            active = db.execute("SELECT path FROM active_project WHERE id=1").fetchone()
            project_path = body.get("project_path") or (active["path"] if active else None)
            cur = db.execute(
                "INSERT INTO tasks (title, description, project_path) VALUES (?,?,?)",
                (title, body.get("description", ""), project_path)
            )
            task_id = cur.lastrowid
            db.commit()
            task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        _broadcast("task_created", task)
        self._send_json(201, task)

    def _append_telemetry(self):
        body = self._read_json()
        with get_db_conn() as db:
            db.execute(
                "INSERT INTO telemetry_events (task_id,event_type,payload) VALUES (?,?,?)",
                (body.get("task_id"), body.get("event_type","generic"), json.dumps(body.get("payload",{})))
            )
            db.commit()
        _broadcast("telemetry", body)
        self._send_json(201, {"ok": True})

    def _get_approvals(self):
        with get_db_conn() as db:
            rows = rows_as_dicts(db.execute(
                "SELECT * FROM approval_gates WHERE status='pending' ORDER BY id"
            ).fetchall())
        self._send_json(200, rows)

    def _post_approval(self):
        body = self._read_json()
        gate_id = body.get("id")
        decision = body.get("decision","")
        if decision not in ("approved", "rejected"):
            return self._send_error(400, "decision must be 'approved' or 'rejected'")
        with get_db_conn() as db:
            gate = db.execute("SELECT * FROM approval_gates WHERE id=?", (gate_id,)).fetchone()
            if not gate:
                return self._send_error(404, "Approval gate not found")
            db.execute(
                "UPDATE approval_gates SET status=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                (decision, gate_id)
            )
            # A 'review_decision' gate is how a human resolves a task that's
            # blocked because REVIEW kept requesting changes past the retry
            # limit (see OrchestrationService._create_review_gate). Approving it
            # means "accept the implementation as-is"; rejecting leaves the task
            # blocked for manual follow-up outside the pipeline.
            if gate["action_type"] == "review_decision" and decision == "approved":
                db.execute("UPDATE tasks SET status='completed' WHERE id=?", (gate["task_id"],))
            db.commit()
            row = dict(db.execute("SELECT * FROM approval_gates WHERE id=?", (gate_id,)).fetchone())
            task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (gate["task_id"],)).fetchone())
        _broadcast("approval_updated", row)
        _broadcast("task_updated", task)
        self._send_json(200, row)

    def _post_artifact(self):
        body = self._read_json()
        required = ["task_id","name","content"]
        for k in required:
            if not body.get(k):
                return self._send_error(400, f"'{k}' is required")
        is_secret, msg = _scrub_secrets(body["content"])
        if is_secret:
            return self._send_error(400, msg)
        path = body.get("path", os.path.join(ALLOWED_WRITE_PREFIX, body["name"]))
        if not _is_safe_path(path):
            return self._send_error(403, "Write path must be inside handoff-lab/")
        with get_db_conn() as db:
            cur = db.execute(
                "INSERT INTO artifacts (task_id,name,path,content) VALUES (?,?,?,?)",
                (body["task_id"], body["name"], path, body["content"])
            )
            artifact_id = cur.lastrowid
            db.commit()
        _broadcast("artifact_created", {"id": artifact_id, "name": body["name"]})
        self._send_json(201, {"id": artifact_id})

    def _get_artifacts(self, task_id: int):
        with get_db_conn() as db:
            rows = rows_as_dicts(db.execute(
                "SELECT * FROM artifacts WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall())
        self._send_json(200, rows)

    def _open_artifact(self, artifact_id: int):
        """
        Export an artifact's content to a real file and hand it to Windows'
        native "Open with" chooser (rundll32 shell32.dll,OpenAs_RunDLL) so the
        user can pick whatever app they want to view it in. `artifacts.path`
        is not reliable for this - for a direct Antigravity run it's just a
        descriptive marker string, and for a manual hand-off it points at a
        file poll_for_returns already deleted after ingesting - so this
        always re-writes `content` fresh into ARTIFACT_EXPORT_DIR instead.
        Exports land under a per-task subfolder so browsing the export
        directory outside the app doesn't dump every task's files together.
        """
        with get_db_conn() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not row:
            return self._send_error(404, "Artifact not found")

        task_dir = os.path.join(ARTIFACT_EXPORT_DIR, f"task_{row['task_id']}")
        os.makedirs(task_dir, exist_ok=True)
        safe_name = os.path.basename(row["name"] or "") or f"artifact_{artifact_id}.txt"
        export_path = os.path.join(task_dir, f"{artifact_id}_{safe_name}")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(row["content"] or "")

        try:
            subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", export_path])
        except (FileNotFoundError, OSError) as e:
            return self._send_error(500, f"Could not open the app picker: {e}")

        self._send_json(200, {"path": export_path})

    def _get_probe(self):
        with get_db_conn() as db:
            rows = rows_as_dicts(db.execute(
                "SELECT * FROM capability_probe ORDER BY probed_at DESC"
            ).fetchall())
        self._send_json(200, rows)

    def _run_probe(self):
        import adapters.probe as probe_mod
        results = probe_mod.run_probe(DB_PATH)
        _broadcast("probe_completed", results)
        self._send_json(200, results)

    def _build_orchestrator(self):
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from domain.agent import AgentName
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from adapters.sse_notifier import SSENotifier
        from adapters.quota_classifier import RegexQuotaClassifier
        from adapters.claude_cli import ClaudeCLIAdapter
        from adapters.antigravity_cli import AntigravityCLIAdapter

        policy_svc = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))
        store = SQLiteCapabilityStore(DB_PATH)
        notifier = SSENotifier(_broadcast)
        classifier = RegexQuotaClassifier()

        invokers = {
            AgentName.CLAUDE: ClaudeCLIAdapter(classifier, store),
            AgentName.ANTIGRAVITY: AntigravityCLIAdapter(classifier, store)
        }
        return OrchestrationService(DB_PATH, policy_svc, store, notifier, invokers)

    def _execute_leg(self, orchestrator, task_id: int, role: str, body: dict) -> dict:
        """Run one leg (PLANNING/EXECUTION/REVIEW) and broadcast its outcome
        over SSE. Returns the result dict; does not write an HTTP response -
        callers (currently only the auto-chaining pipeline loop) do that."""
        from domain.agent import Role

        try:
            result = orchestrator.run_leg(task_id, Role(role), body)
        except Exception as e:
            result = {"status": "failed", "agent": role, "reason": str(e)}

        if result.get("status") == "completed":
            agent = result.get("agent")
            if agent == "antigravity":
                _broadcast("antigravity_status", {"task_id": task_id, "role": role, "status": "completed", **result.get("result", {})})
            else:
                _broadcast(f"{agent}_completed", {"task_id": task_id, "role": role, **result.get("result", {})})
        elif result.get("status") == "blocked":
            _broadcast("antigravity_status", {"task_id": task_id, "role": role, "status": "blocked", "reason": result.get("reason")})
        elif result.get("status") not in ("skipped_degraded",):
            agent = result.get("agent", role)
            _broadcast(f"{agent}_failed", {"task_id": task_id, "role": role, "error": result.get("reason")})

        return result


    def _poll_antigravity(self, task_id: int):
        import adapters.antigravity_cli as ag_mod

        with get_db_conn() as db:
            run = db.execute(
                "SELECT id FROM agent_runs WHERE task_id=? AND agent_name='antigravity' ORDER BY id DESC LIMIT 1",
                (task_id,)
            ).fetchone()
        if not run:
            return self._send_error(404, "No Antigravity run found for task")

        found = ag_mod.poll_for_returns(task_id, run["id"], DB_PATH)
        if found:
            _broadcast("antigravity_completed", {"task_id": task_id, "artifacts": found})
        self._send_json(200, {"artifacts_found": len(found), "paths": found})

    # -- Delegate Routing (autonomous, single-button) --------------------------

    MAX_PIPELINE_STEPS = 8  # hard safety cap, independent of REVIEW_RETRY_LIMIT

    def _delegate(self, task_id: int):
        """
        The dashboard's only "run a task" action. No role picker: the
        orchestrator decides the next stage for this task (PLANNING ->
        EXECUTION -> REVIEW, looping back to EXECUTION if REVIEW requests
        changes, capped retries) and this loop keeps calling it until the
        task is done or genuinely needs a human (HOLD / failed / retries
        exhausted). Each leg's outcome is broadcast over SSE as it happens,
        so the UI gets live progress even though this request blocks for the
        whole chain.
        """
        is_secret, msg = _scrub_secrets(json.dumps(self._read_json() or {}))
        if is_secret:
            return self._send_error(400, msg)

        with get_db_conn() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return self._send_error(404, "Task not found")

        orchestrator = self._build_orchestrator()
        steps = []
        for _ in range(self.MAX_PIPELINE_STEPS):
            role = orchestrator.next_role(task_id)
            if role is None:
                break
            result = self._execute_leg(orchestrator, task_id, role.value, {})
            steps.append({"role": role.value, **result})
            if result.get("status") in ("blocked", "failed"):
                break

        final_status = steps[-1]["status"] if steps else "noop"
        self._send_json(200, {"steps": steps, "final_status": final_status})

    # -- Project selection ------------------------------------------------------

    def _get_project(self):
        with get_db_conn() as db:
            row = db.execute("SELECT * FROM active_project WHERE id=1").fetchone()
        self._send_json(200, dict(row) if row else None)

    def _set_project(self):
        body = self._read_json()
        path = (body.get("path") or "").strip()
        if not path:
            return self._send_error(400, "'path' is required")
        if not os.path.isdir(path):
            return self._send_error(400, f"Not a directory: {path}")
        name = (body.get("name") or os.path.basename(os.path.normpath(path)) or path)
        with get_db_conn() as db:
            db.execute(
                """INSERT INTO active_project (id, path, name) VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET path=excluded.path, name=excluded.name,
                                                  selected_at=CURRENT_TIMESTAMP""",
                (path, name)
            )
            db.commit()
            row = dict(db.execute("SELECT * FROM active_project WHERE id=1").fetchone())
        _broadcast("project_selected", row)
        self._send_json(200, row)

    def _fs_list(self, query: dict):
        """Directory listing for the in-app folder browser. Folders only -
        this is a project picker, not a general file manager."""
        req_path = (query.get("path", [""])[0] or "").strip()
        try:
            if not req_path:
                return self._send_json(200, {"path": "", "parent": None, "entries": _list_roots()})
            if not os.path.isdir(req_path):
                return self._send_error(400, f"Not a directory: {req_path}")
            entries = []
            for name in sorted(os.listdir(req_path), key=str.lower):
                full = os.path.join(req_path, name)
                try:
                    if os.path.isdir(full):
                        entries.append({"name": name, "path": full})
                except OSError:
                    continue
            parent = os.path.dirname(req_path.rstrip("\\/"))
            if not parent or parent == req_path:
                parent = ""  # signals "show drive/root list"
            self._send_json(200, {"path": req_path, "parent": parent, "entries": entries})
        except PermissionError:
            self._send_error(403, "Permission denied")
        except Exception as e:
            self._send_error(500, str(e))

    # -- Capability override ------------------------------------------------------

    def _reset_capability(self):
        """
        Manual escape hatch for low-confidence / misfired quota classifications
        (e.g. Codex's documented 'phantom limit' bug where /status disagrees
        with the actual error). Clears EXHAUSTED + cooldown_until for an agent
        immediately instead of making a human wait out a guessed cooldown.
        """
        from domain.agent import AgentName
        from domain.capability import AgentCapability, QuotaStatus
        from adapters.sqlite_capability_store import SQLiteCapabilityStore

        body = self._read_json()
        try:
            agent = AgentName((body.get("agent") or "").strip().lower())
        except ValueError:
            return self._send_error(400, "'agent' must be one of claude, antigravity")

        store = SQLiteCapabilityStore(DB_PATH)
        cap = store.get_capability(agent) or AgentCapability(installed=True, quota_status=QuotaStatus.UNKNOWN)
        cap.quota_status = QuotaStatus.AVAILABLE
        cap.cooldown_until = None
        cap.quota_confidence = 1.0
        store.save_capability(agent, cap)
        _broadcast("capability_reset", {"agent": agent.value})
        self._send_json(200, {"agent": agent.value, "quota_status": "available"})

    def _sse_stream(self):
        import queue
        q = queue.Queue(maxsize=SSE_QUEUE_MAXSIZE)
        with _sse_lock:
            _sse_subscribers.append(q)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # Send a heartbeat immediately
        try:
            self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()
        except Exception:
            return

        while True:
            try:
                msg = q.get(timeout=SSE_HEARTBEAT_TIMEOUT)
                self.wfile.write(msg)
                self.wfile.flush()
            except Exception:
                # Timeout or disconnect - send keepalive ping
                try:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except Exception:
                    break

        with _sse_lock:
            if q in _sse_subscribers:
                _sse_subscribers.remove(q)


# -- entry point ------------------------------------------------------------------

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    init_db()
    print(f"  Open Engine Handoff Backend")
    print(f"  URL:         http://localhost:{port}")
    print(f"  DB:          {DB_PATH}")
    print(f"  handoff-lab: {ALLOWED_WRITE_PREFIX}")
    print(f"  Press Ctrl+C to stop.\n")
    server = http.server.ThreadingHTTPServer(("", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")


if __name__ == "__main__":
    main()
