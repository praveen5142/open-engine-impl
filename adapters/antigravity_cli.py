import subprocess
import json
import sqlite3
import os
import shutil
import re
from datetime import datetime

from ports.agent_invocation import AgentInvocationPort
from domain.agent import AgentName
from domain.errors import AgentQuotaExhaustedError, AgentUnavailableError
from adapters.quota_classifier import RegexQuotaClassifier
from ports.capability_store import CapabilityStorePort
from domain.capability import QuotaStatus

HANDOFF_LAB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "handoff-lab"))
# INBOX_FILE and RETURN_MARKER will be generated dynamically per task

# CLI ships as `agy`; some installs alias it as `antigravity`. Try both.
CLI_CANDIDATES = ["agy", "agy.exe", "agy.cmd", "antigravity", "antigravity.exe", "antigravity.cmd"]

EXECUTION_TIMEOUT_SECONDS = 900
MAX_STDERR_CHARS = 300
MAX_STDOUT_CHARS = 1000


def _find_cli() -> str | None:
    for name in CLI_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
            
    home = os.path.expanduser("~")
    common_paths = [
        os.path.join(home, "AppData", "Local", "agy", "bin"),
        os.path.join(home, "AppData", "Local", "Programs", "Antigravity", "bin"),
        os.path.join(home, ".local", "bin"),
    ]
    
    for path in common_paths:
        for name in CLI_CANDIDATES:
            full_path = os.path.join(path, name)
            if os.path.isfile(full_path) and os.access(full_path, os.X_OK):
                return full_path
                
    return None


def _write_inbox(baton_id, work_order: dict, reason: str, task_id: int) -> str:
    """
    Write a manual hand-off artifact so a human can relay the work order to
    Antigravity by hand and drop the result back in handoff-lab/. This is the
    EXECUTION leg's only safety net — routing.json gives it no fallback agent
    and no degraded mode, so silently failing here would strand the task with
    no way forward.
    """
    os.makedirs(HANDOFF_LAB, exist_ok=True)
    inbox_payload = {
        "schema_version": "1.0",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "baton_id": baton_id,
        "from_agent": "claude",
        "to_agent": "antigravity",
        "work_order": work_order,
        "reason": reason,
        "instructions": (
            "Antigravity CLI was not reachable. Please process this work order "
            "manually and drop return artifact file(s) in this same directory "
            f"({HANDOFF_LAB}) with filenames starting with 'antigravity_return_{task_id}_'. "
            "The backend will pick them up via GET /api/run/<task_id>/poll."
        ),
    }
    safe_task_id = re.sub(r'[^a-zA-Z0-9_-]', '', str(task_id))
    inbox_file = os.path.join(HANDOFF_LAB, f"antigravity_inbox_{safe_task_id}.json")
    with open(inbox_file, "w", encoding="utf-8") as f:
        json.dump(inbox_payload, f, indent=2)
    return inbox_file


class AntigravityCLIAdapter(AgentInvocationPort):
    def __init__(self, classifier: RegexQuotaClassifier, store: CapabilityStorePort):
        self.classifier = classifier
        self.store = store

    def invoke(self, task_id: int, role: str, db_path: str, payload: dict) -> dict:
        baton_id = payload.get("baton_id")
        work_order = payload.get("work_order", {})

        cap = self.store.get_capability(AgentName.ANTIGRAVITY)
        agy_path = _find_cli()

        if not agy_path:
            inbox_path = _write_inbox(baton_id, work_order, "Antigravity CLI not found on PATH", task_id)
            raise AgentUnavailableError(
                "Antigravity CLI not found on PATH", context={"inbox": inbox_path}
            )

        conn = sqlite3.connect(db_path)
        proj_row = conn.execute("SELECT path FROM active_project WHERE id=1").fetchone()
        conn.close()
        project_dir = proj_row[0] if proj_row else os.getcwd()

        # agy has no non-interactive flag equivalent to `--stdin`; the real CLI
        # (installed separately from the Antigravity IDE - the binary is `agy`,
        # not `antigravity`) takes a natural-language prompt via `--print`/`-p`
        # and needs `--add-dir` to point it at the target project, since its
        # notion of "workspace" is independent of the subprocess's cwd (verified
        # live: without --add-dir it silently wrote into its own
        # ~/.gemini/antigravity-cli/scratch/ directory instead of the project).
        prompt = (
            "You are Antigravity acting as the EXECUTION agent in an automated "
            "handoff pipeline. Implement the work order below directly in this "
            "project's files.\n\n## Work Order\n" + json.dumps(work_order, indent=2)
        )

        try:
            # --sandbox + toolPermission=proceed-in-sandbox (configured in
            # ~/.gemini/antigravity-cli/settings.json) lets this run headlessly
            # without --dangerously-skip-permissions, which is a blanket
            # permission-bypass flag we deliberately do not use here.
            #
            # cwd=project_dir is a defensive addition: claude_cli.py had the
            # same class of bug (--add-dir alone did not redirect Claude away
            # from the server process's own cwd), and every prior test of
            # this adapter happened to have the active project match wherever
            # server.py itself runs from, so that confound could easily have
            # masked the same issue here even though it "worked" in testing.
            result = subprocess.run(
                [agy_path, "--print", "--sandbox", "--add-dir", project_dir, "--print-timeout", "15m"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=EXECUTION_TIMEOUT_SECONDS,
                cwd=project_dir,
            )
        except FileNotFoundError:
            inbox_path = _write_inbox(baton_id, work_order, "Antigravity CLI failed to start", task_id)
            raise AgentUnavailableError(
                "Antigravity CLI failed to start", context={"inbox": inbox_path}
            )
        except subprocess.TimeoutExpired:
            inbox_path = _write_inbox(baton_id, work_order, f"Antigravity CLI timed out after {EXECUTION_TIMEOUT_SECONDS}s", task_id)
            raise AgentUnavailableError(
                f"Antigravity CLI timed out after {EXECUTION_TIMEOUT_SECONDS}s", context={"inbox": inbox_path}
            )

        signal = self.classifier.classify(result.returncode, result.stdout, result.stderr)
        if cap:
            cap.quota_status = signal.status
            if signal.status == QuotaStatus.EXHAUSTED:
                cap.cooldown_until = signal.retry_after
            self.store.save_capability(AgentName.ANTIGRAVITY, cap)

        if signal.status == QuotaStatus.EXHAUSTED:
            raise AgentQuotaExhaustedError("Antigravity usage limit reached")

        if result.returncode != 0:
            inbox_path = _write_inbox(baton_id, work_order, f"Antigravity exited with an error: {result.stderr[:MAX_STDERR_CHARS]}", task_id)
            raise AgentUnavailableError(
                f"Antigravity failed: {result.stderr[:MAX_STDERR_CHARS]}", context={"inbox": inbox_path}
            )

        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "INSERT INTO agent_runs (task_id, agent_name, role, status, started_at, completed_at, logs) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
            (task_id, "antigravity", role, "completed", json.dumps({"stdout": result.stdout[:MAX_STDOUT_CHARS]}))
        )
        run_id = cur.lastrowid
        conn.execute(
            "INSERT INTO artifacts (task_id, name, path, content) VALUES (?, ?, ?, ?)",
            (
                task_id,
                f"antigravity_output_{task_id}_{run_id}.txt",
                f"captured_stdout:task_{task_id}_run_{run_id}",
                result.stdout
            )
        )
        conn.commit()
        conn.close()
        
        return {"run_id": run_id, "status": "completed"}

def poll_for_returns(task_id: int, agent_run_id: int, db_path: str = "db.sqlite") -> list:
    if not os.path.isdir(HANDOFF_LAB):
        return []
    found = []
    prefix = f"antigravity_return_{task_id}_"
    for f in os.listdir(HANDOFF_LAB):
        if f.startswith(prefix):
            found.append(os.path.join(HANDOFF_LAB, f))
            
    if not found:
        return []

    conn = sqlite3.connect(db_path)
    ingested = []
    for fpath in found:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        conn.execute(
            "INSERT INTO artifacts (task_id, name, path, content) VALUES (?,?,?,?)",
            (task_id, os.path.basename(fpath), fpath, content)
        )
        ingested.append(fpath)

    if ingested:
        conn.execute(
            "UPDATE agent_runs SET status='completed', completed_at=CURRENT_TIMESTAMP, logs=? WHERE id=?",
            (json.dumps({"return_artifacts": ingested}), agent_run_id)
        )
        
        # Clean up files after successful ingestion
        for fpath in ingested:
            try:
                os.remove(fpath)
            except OSError:
                pass
                
    conn.commit()
    conn.close()
    return ingested
