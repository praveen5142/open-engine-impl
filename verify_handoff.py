"""
verify_handoff.py  -  Automated Test Suite for Open Engine Handoff MVP
"""
import os
import sys
import json
import sqlite3
import tempfile
import shutil
import uuid
import unittest
import subprocess
import time
import re
import gc
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Windows: reconfigure stdout to UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# -- Setup temp DB for isolation --------------------------------------------

def make_temp_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    schema = open(os.path.join(BASE_DIR, "db_schema.sql")).read()
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return tmp.name

def remove_db(path):
    gc.collect()
    for _ in range(6):
        try:
            os.unlink(path)
            for ext in ('-wal', '-shm'):
                p = path + ext
                if os.path.isfile(p):
                    os.unlink(p)
            return
        except PermissionError:
            time.sleep(0.25)

# -- Tests --------------------------------------------------------------------

class TestDBInit(unittest.TestCase):
    def test_schema_creates(self):
        db_path = make_temp_db()
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        remove_db(db_path)
        expected = {"tasks","agent_runs",
                    "telemetry_events","approval_gates","artifacts","capability_probe", "routing_decisions"}
        self.assertEqual(expected, tables & expected)

class TestSecretScrubber(unittest.TestCase):
    SECRET_PATTERNS = [
        r"(?i)(api[_-]?key|secret|token|password|auth)[=:]\s*\S+",
        r"(?i)bearer\s+[A-Za-z0-9\-_\.]+",
        r"(?i)sk-[A-Za-z0-9]+",
        r"BEGIN\s+(RSA\s+)?PRIVATE\s+KEY",
    ]

    def _scrub(self, text):
        for p in self.SECRET_PATTERNS:
            if re.search(p, text):
                return True
        return False

    def test_api_key_rejected(self):
        self.assertTrue(self._scrub("api_key=sk-abc123xyz"))

    def test_bearer_token_rejected(self):
        self.assertTrue(self._scrub("Authorization: bearer eyJhbGciOiJIUzI1NiJ9"))

    def test_private_key_rejected(self):
        self.assertTrue(self._scrub("-----BEGIN RSA PRIVATE KEY-----"))

    def test_normal_text_passes(self):
        self.assertFalse(self._scrub("This is a research note about Python stdlib."))

    def test_openai_key_rejected(self):
        self.assertTrue(self._scrub("sk-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"))

class TestSafetyPath(unittest.TestCase):
    ALLOWED = os.path.abspath(os.path.join(BASE_DIR, "handoff-lab"))

    def _is_safe(self, path):
        return os.path.abspath(path).startswith(self.ALLOWED)

    def test_handoff_lab_is_allowed(self):
        self.assertTrue(self._is_safe(os.path.join(self.ALLOWED, "output.json")))

    def test_parent_dir_blocked(self):
        self.assertFalse(self._is_safe(os.path.join(BASE_DIR, "output.json")))

    def test_traversal_blocked(self):
        self.assertFalse(self._is_safe(os.path.join(self.ALLOWED, "..", "..", "secret.txt")))

class TestRouting(unittest.TestCase):
    def setUp(self):
        self.db = make_temp_db()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO tasks (title) VALUES ('test task')")
        conn.commit()
        conn.close()
        self.task_id = 1

    def tearDown(self):
        remove_db(self.db)

    @patch('subprocess.run')
    def test_quota_exhaustion_holds_no_fallback(self, mock_run):
        """
        Since the RESEARCH/Codex leg was removed, no role has a fallback
        agent anymore (config/routing.json). A quota-exhausted primary must
        now HOLD instead of falling back to a second agent.
        """
        from domain.agent import Role, AgentName
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore

        class DummyNotifier:
            def __init__(self):
                self.held = False
            def notify_hold(self, task_id, role, reason):
                self.held = True

        store = SQLiteCapabilityStore(self.db)
        from domain.capability import AgentCapability, QuotaStatus
        store.save_capability(AgentName.CLAUDE, AgentCapability(installed=True, quota_status=QuotaStatus.EXHAUSTED, cooldown_until=time.time()+3600))

        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))
        decision = policy.route(Role.PLANNING, store.get_capability)
        self.assertIsNone(decision.agent)
        self.assertTrue(decision.hold)

        notifier = DummyNotifier()
        orchestrator = OrchestrationService(self.db, policy, store, notifier, {})
        res = orchestrator.run_leg(self.task_id, Role.PLANNING, {})
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(notifier.held)

    def test_planning_holds_when_claude_unavailable(self):
        """
        PLANNING has no fallback and degraded_allowed=false (there's nothing
        sensible left to degrade to once RESEARCH/Codex is gone), so an
        unavailable Claude must HOLD rather than silently skip the leg.
        """
        from domain.agent import Role, AgentName
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore

        store = SQLiteCapabilityStore(self.db)
        # Claude is NOT installed
        from domain.capability import AgentCapability, QuotaStatus
        store.save_capability(AgentName.CLAUDE, AgentCapability(installed=False, quota_status=QuotaStatus.UNKNOWN))

        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))

        class DummyNotifier:
            def __init__(self):
                self.held = False
            def notify_hold(self, task_id, role, reason):
                self.held = True

        notifier = DummyNotifier()
        orchestrator = OrchestrationService(
            self.db, policy, store, notifier, {}
        )

        res = orchestrator.run_leg(self.task_id, Role.PLANNING, {})
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(notifier.held)

    def test_probe_preserves_quota_state_across_runs(self):
        """
        Regression test for the bug found in code review: adapters/probe.py
        used to INSERT a fresh capability_probe row on every run without the
        quota columns, which silently reset an EXHAUSTED+cooldown state back
        to 'unknown' the next time someone ran the Phase 0 probe - defeating
        the whole cooldown / probe-before-invoke cost control.
        """
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from adapters.probe import run_probe
        from domain.agent import AgentName
        from domain.capability import AgentCapability, QuotaStatus

        store = SQLiteCapabilityStore(self.db)
        cooldown = time.time() + 3600
        store.save_capability(
            AgentName.ANTIGRAVITY,
            AgentCapability(installed=True, quota_status=QuotaStatus.EXHAUSTED, cooldown_until=cooldown),
        )

        # Re-running the capability probe must not erase the exhausted/cooldown
        # state it doesn't itself have the information to re-derive.
        run_probe(self.db)

        cap = store.get_capability(AgentName.ANTIGRAVITY)
        self.assertEqual(cap.quota_status, QuotaStatus.EXHAUSTED)
        self.assertIsNotNone(cap.cooldown_until)
        self.assertAlmostEqual(cap.cooldown_until, cooldown, delta=1)

    def test_probe_search_names_include_agy_for_antigravity(self):
        """
        Regression test: antigravity_cli.py invokes the binary as `agy`, so
        the probe's search list for the 'antigravity' capability row must
        include agy/agy.exe/agy.cmd, or a real agy install gets reported as
        'antigravity: not found' and EXECUTION permanently HOLDs.
        """
        from adapters.probe import SEARCH_NAMES
        self.assertIn("agy", SEARCH_NAMES.get("antigravity", []))
        self.assertNotIn("agy", SEARCH_NAMES.keys(), "agy should not be a separate, unused tool entry")

    @patch('adapters.antigravity_cli._find_cli', return_value=None)
    def test_antigravity_unavailable_writes_inbox_and_blocks_gracefully(self, mock_find_cli):
        """
        Regression test: the original antigravity.py wrote a manual hand-off
        inbox artifact and returned a graceful 'blocked' status when the CLI
        wasn't reachable. That safety net was dropped when antigravity_cli.py
        was introduced (any missing-CLI turned into a bare 'failed'). This
        confirms it's restored - EXECUTION has no fallback agent, so losing
        this hand-off would strand a task with no way forward.
        """
        from domain.agent import Role, AgentName
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from adapters.quota_classifier import RegexQuotaClassifier
        from adapters.antigravity_cli import AntigravityCLIAdapter
        from domain.capability import AgentCapability, QuotaStatus

        # This test writes a real inbox artifact to handoff-lab/ (that's the
        # behavior under test). Back up whatever is there so we don't clobber
        # a real hand-off in progress.
        inbox_path = os.path.join(BASE_DIR, "handoff-lab", "antigravity_inbox.json")
        backup = None
        if os.path.isfile(inbox_path):
            with open(inbox_path, "r", encoding="utf-8") as f:
                backup = f.read()
        self.addCleanup(lambda: (
            open(inbox_path, "w", encoding="utf-8").write(backup) if backup is not None
            else (os.remove(inbox_path) if os.path.isfile(inbox_path) else None)
        ))

        store = SQLiteCapabilityStore(self.db)
        store.save_capability(AgentName.ANTIGRAVITY, AgentCapability(installed=True, quota_status=QuotaStatus.AVAILABLE))

        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))
        classifier = RegexQuotaClassifier()
        agy_adapter = AntigravityCLIAdapter(classifier, store)

        class DummyNotifier:
            def __init__(self): self.held = False
            def notify_hold(self, task_id, role, reason): self.held = True

        notifier = DummyNotifier()
        orchestrator = OrchestrationService(
            self.db, policy, store, notifier,
            {AgentName.CLAUDE: MagicMock(), AgentName.ANTIGRAVITY: agy_adapter}
        )

        result = orchestrator.run_leg(self.task_id, Role.EXECUTION, {})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("inbox", result)
        self.assertTrue(os.path.isfile(result["inbox"]))
        self.assertTrue(notifier.held)

    def test_execution_auto_resolves_work_order_from_planning(self):
        """
        EXECUTION has no fallback/degraded path anymore (RESEARCH/batons are
        gone), so its only source of a work order is the latest completed
        PLANNING run. This confirms OrchestrationService actually resolves
        that automatically - the single 'Delegate Routing' button depends on
        this to chain PLANNING -> EXECUTION without a human copying JSON
        between legs.
        """
        from domain.agent import Role, AgentName
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore

        store = SQLiteCapabilityStore(self.db)
        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))

        work_order = {"work_order_id": "wo-1", "task_summary": "Ship the widget", "implementation_steps": ["do it"]}
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO agent_runs (task_id, agent_name, role, status, logs) VALUES (?,?,?,?,?)",
            (self.task_id, "claude", "PLANNING", "completed", json.dumps(work_order))
        )
        conn.commit()
        conn.close()

        class DummyNotifier:
            def notify_hold(self, task_id, role, reason): pass

        captured = {}
        antigravity_mock = MagicMock()
        antigravity_mock.invoke.side_effect = lambda tid, role, db, payload: captured.update(payload) or {"run_id": 1}

        orchestrator = OrchestrationService(
            self.db, policy, store, DummyNotifier(),
            {AgentName.ANTIGRAVITY: antigravity_mock}
        )
        # Antigravity must appear installed/available for routing to pick it.
        from domain.capability import AgentCapability, QuotaStatus
        store.save_capability(AgentName.ANTIGRAVITY, AgentCapability(installed=True, quota_status=QuotaStatus.AVAILABLE))

        result = orchestrator.run_leg(self.task_id, Role.EXECUTION, {})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(captured["work_order"]["task_summary"], "Ship the widget")

    def test_hold_on_antigravity(self):
        from domain.agent import Role, AgentName
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore

        store = SQLiteCapabilityStore(self.db)
        # Antigravity is NOT installed
        from domain.capability import AgentCapability, QuotaStatus
        store.save_capability(AgentName.ANTIGRAVITY, AgentCapability(installed=False, quota_status=QuotaStatus.UNKNOWN))

        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))

        class DummyNotifier:
            def __init__(self):
                self.held = False
            def notify_hold(self, task_id, role, reason):
                self.held = True

        notifier = DummyNotifier()
        orchestrator = OrchestrationService(
            self.db, policy, store, notifier, {}
        )

        res = orchestrator.run_leg(self.task_id, Role.EXECUTION, {})
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(notifier.held)

    def _insert_run(self, agent_name, role, status, logs=None):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO agent_runs (task_id, agent_name, role, status, logs) VALUES (?,?,?,?,?)",
            (self.task_id, agent_name, role, status, json.dumps(logs) if logs is not None else None)
        )
        conn.commit()
        conn.close()

    def _make_orchestrator(self):
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore

        class DummyNotifier:
            def notify_hold(self, task_id, role, reason): pass

        store = SQLiteCapabilityStore(self.db)
        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))
        return OrchestrationService(self.db, policy, store, DummyNotifier(), {})

    def test_next_role_walks_planning_execution_review(self):
        """next_role() should drive RESEARCH -> SPEC -> PLANNING -> EXECUTION -> REVIEW
        (the full RSPBV pipeline) for a fresh task — this is what the
        single-button auto-chain (_delegate loop in server.py) relies on."""
        from domain.agent import Role

        orchestrator = self._make_orchestrator()
        # A fresh task should start with RESEARCH
        self.assertEqual(orchestrator.next_role(self.task_id), Role.RESEARCH)

        self._insert_run("engine", "RESEARCH", "completed", {"snippets": []})
        self.assertEqual(orchestrator.next_role(self.task_id), Role.SPEC)

        self._insert_run("claude", "SPEC", "completed", {"acceptance_criteria": ["works"]})
        self.assertEqual(orchestrator.next_role(self.task_id), Role.PLANNING)

        self._insert_run("claude", "PLANNING", "completed", {"task_summary": "x"})
        self.assertEqual(orchestrator.next_role(self.task_id), Role.EXECUTION)

        self._insert_run("antigravity", "EXECUTION", "completed", {"stdout": "done"})
        self.assertEqual(orchestrator.next_role(self.task_id), Role.REVIEW)

    def test_next_role_loops_back_on_changes_requested_then_stops_at_limit(self):
        """A 'changes_requested' verdict should route back to EXECUTION, and
        that loop must stop after REVIEW_RETRY_LIMIT cycles instead of
        spinning forever - this is the safeguard for the review gate."""
        from domain.agent import Role
        from application.orchestration_service import REVIEW_RETRY_LIMIT

        orchestrator = self._make_orchestrator()
        # Insert RESEARCH+SPEC prereqs so next_role can reach PLANNING
        self._insert_run("engine", "RESEARCH", "completed", {"snippets": []})
        self._insert_run("claude", "SPEC", "completed", {"acceptance_criteria": ["works"]})
        self._insert_run("claude", "PLANNING", "completed", {"task_summary": "x"})
        self._insert_run("antigravity", "EXECUTION", "completed", {"stdout": "done"})
        self._insert_run("claude", "REVIEW", "completed", {"verdict": "changes_requested", "feedback": "fix it"})

        self.assertEqual(orchestrator.next_role(self.task_id), Role.EXECUTION)

        # Simulate REVIEW_RETRY_LIMIT total review cycles, all still requesting changes.
        for _ in range(REVIEW_RETRY_LIMIT - 1):
            self._insert_run("antigravity", "EXECUTION", "completed", {"stdout": "done"})
            self._insert_run("claude", "REVIEW", "completed", {"verdict": "changes_requested", "feedback": "still broken"})

        self.assertIsNone(orchestrator.next_role(self.task_id))
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (self.task_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], "blocked")

    def test_review_gate_created_when_retries_exhausted_and_not_duplicated(self):
        """
        Regression test: a 'blocked' task with exhausted REVIEW retries used
        to have no actionable control anywhere in the UI - the Approval
        Gates panel always said 'No pending approvals'. next_role() must now
        create a 'review_decision' gate exactly once, even if next_role() is
        called again later (e.g. the user clicks Run Task again while blocked).
        """
        from application.orchestration_service import REVIEW_RETRY_LIMIT

        orchestrator = self._make_orchestrator()
        # Insert RESEARCH+SPEC prereqs so next_role can reach PLANNING
        self._insert_run("engine", "RESEARCH", "completed", {"snippets": []})
        self._insert_run("claude", "SPEC", "completed", {"acceptance_criteria": ["works"]})
        self._insert_run("claude", "PLANNING", "completed", {"task_summary": "x"})
        for _ in range(REVIEW_RETRY_LIMIT):
            self._insert_run("antigravity", "EXECUTION", "completed", {"stdout": "done"})
            self._insert_run("claude", "REVIEW", "completed", {"verdict": "changes_requested", "feedback": "still broken"})

        self.assertIsNone(orchestrator.next_role(self.task_id))
        self.assertIsNone(orchestrator.next_role(self.task_id))  # simulate a second click while blocked

        conn = sqlite3.connect(self.db)
        gates = conn.execute(
            "SELECT action_type, status, payload FROM approval_gates WHERE task_id=?", (self.task_id,)
        ).fetchall()
        conn.close()
        self.assertEqual(len(gates), 1, "should not create a duplicate gate on repeat next_role() calls")
        self.assertEqual(gates[0][0], "review_decision")
        self.assertEqual(gates[0][1], "pending")
        self.assertIn("still broken", gates[0][2])

    def test_parse_claude_json_extracts_verdict_from_prose_with_fenced_block(self):
        """
        Regression test for a real bug found via an actual end-to-end
        pipeline run: REVIEW's assistant text is prose ("Here's what I
        verified...") ending in a ```json fenced block, not pure JSON. The
        old _parse_claude_json only handled pure-JSON text and otherwise
        silently fell back to re-parsing the *outer* envelope (which has no
        "verdict" key), so every real REVIEW got misclassified as
        changes_requested even though Claude's actual text said "approved" -
        burning the entire retry budget on a phantom rejection.
        """
        from adapters.claude_cli import ClaudeCLIAdapter

        adapter = ClaudeCLIAdapter(classifier=None, store=None)
        raw = json.dumps({
            "type": "result",
            "result": (
                "All fixes look correct. Here's what I verified:\n\n"
                "**Fix 1**: looks good.\n\n"
                "```json\n"
                '{"verdict": "approved", "feedback": "Everything matches the work order."}\n'
                "```"
            ),
        })
        parsed = adapter._parse_claude_json(raw)
        self.assertEqual(parsed.get("verdict"), "approved")
        self.assertEqual(parsed.get("feedback"), "Everything matches the work order.")

    @patch('subprocess.run')
    def test_planning_rejects_empty_work_order_instead_of_completing(self, mock_run):
        """
        Regression test for a real stuck-loop bug: a task with no description
        ("hello world", title only) made Claude return no usable work order,
        _parse_claude_json correctly returned {}, and the old code stored
        that {} as a "completed" PLANNING run anyway. Since PLANNING only
        runs once per task, every EXECUTION retry after that kept re-feeding
        Antigravity the same empty plan forever. PLANNING must now fail
        loudly instead of silently "succeeding" with nothing.
        """
        from adapters.claude_cli import ClaudeCLIAdapter
        from adapters.quota_classifier import RegexQuotaClassifier
        from adapters.sqlite_capability_store import SQLiteCapabilityStore

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET title='hello world', description='' WHERE id=?", (self.task_id,))
        conn.commit()
        conn.close()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"type": "result", "result": "Sure, tell me more about what you'd like done."})
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        store = SQLiteCapabilityStore(self.db)
        adapter = ClaudeCLIAdapter(RegexQuotaClassifier(), store)
        with self.assertRaises(RuntimeError):
            adapter.invoke(self.task_id, "PLANNING", self.db, {})

        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE task_id=? AND role='PLANNING'", (self.task_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 0, "an empty work order must not be recorded as a completed PLANNING run")

    def test_execution_fails_fast_on_empty_work_order_without_invoking_antigravity(self):
        """
        Defense-in-depth companion to the PLANNING guard above: even if an
        empty-work-order PLANNING run already exists (e.g. from before that
        fix), EXECUTION must refuse to invoke Antigravity against it instead
        of burning a real CLI call and a REVIEW cycle on a foregone
        conclusion.
        """
        from domain.agent import Role, AgentName
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from domain.capability import AgentCapability, QuotaStatus

        store = SQLiteCapabilityStore(self.db)
        store.save_capability(AgentName.ANTIGRAVITY, AgentCapability(installed=True, quota_status=QuotaStatus.AVAILABLE))
        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))

        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO agent_runs (task_id, agent_name, role, status, logs) VALUES (?,?,?,?,?)",
            (self.task_id, "claude", "PLANNING", "completed", json.dumps({}))
        )
        conn.commit()
        conn.close()

        class DummyNotifier:
            def notify_hold(self, task_id, role, reason): pass

        antigravity_mock = MagicMock()
        orchestrator = OrchestrationService(
            self.db, policy, store, DummyNotifier(), {AgentName.ANTIGRAVITY: antigravity_mock}
        )

        result = orchestrator.run_leg(self.task_id, Role.EXECUTION, {})
        self.assertEqual(result["status"], "failed")
        antigravity_mock.invoke.assert_not_called()

    def test_next_role_none_when_review_approves(self):
        from domain.agent import Role

        orchestrator = self._make_orchestrator()
        # Insert RESEARCH+SPEC prereqs so next_role can reach PLANNING/EXECUTION/REVIEW
        self._insert_run("engine", "RESEARCH", "completed", {"snippets": []})
        self._insert_run("claude", "SPEC", "completed", {"acceptance_criteria": ["works"]})
        self._insert_run("claude", "PLANNING", "completed", {"task_summary": "x"})
        self._insert_run("antigravity", "EXECUTION", "completed", {"stdout": "done"})
        self._insert_run("claude", "REVIEW", "completed", {"verdict": "approved", "feedback": "looks good"})

        self.assertIsNone(orchestrator.next_role(self.task_id))
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (self.task_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], "completed")

    @patch('adapters.antigravity_cli._find_cli', return_value="/fake/path/agy")
    @patch('subprocess.run')
    def test_successful_execution_inserts_artifact(self, mock_run, mock_find_cli):
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from adapters.quota_classifier import RegexQuotaClassifier
        from adapters.antigravity_cli import AntigravityCLIAdapter
        from domain.capability import AgentCapability, QuotaStatus
        from domain.agent import AgentName

        store = SQLiteCapabilityStore(self.db)
        store.save_capability(AgentName.ANTIGRAVITY, AgentCapability(installed=True, quota_status=QuotaStatus.AVAILABLE))

        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "This is the mocked stdout content"
        mock_res.stderr = ""
        mock_run.return_value = mock_res

        classifier = RegexQuotaClassifier()
        adapter = AntigravityCLIAdapter(classifier, store)

        payload = {
            "baton_id": "baton-123",
            "work_order": {"some": "data"}
        }

        res = adapter.invoke(self.task_id, "EXECUTION", self.db, payload)
        run_id = res["run_id"]

        conn = sqlite3.connect(self.db)
        artifact_rows = conn.execute("SELECT task_id, name, path, content FROM artifacts WHERE task_id=?", (self.task_id,)).fetchall()
        conn.close()

        self.assertEqual(len(artifact_rows), 1)
        task_id_got, name_got, path_got, content_got = artifact_rows[0]
        self.assertEqual(task_id_got, self.task_id)
        self.assertEqual(content_got, "This is the mocked stdout content")
        self.assertIn(str(self.task_id), name_got)
        self.assertIn(str(run_id), name_got)



# -- Phase 2: RSPBV Pipeline Tests ---------------------------------------------

class TestRSPBVPipeline(unittest.TestCase):
    """Tests for the Phase 2 RESEARCH -> SPEC -> PLANNING -> EXECUTION -> REVIEW pipeline."""

    def setUp(self):
        self.db = make_temp_db()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO tasks (title, description) VALUES ('test task', 'A test description')")
        conn.commit()
        conn.close()
        self.task_id = 1

    def tearDown(self):
        remove_db(self.db)

    def _insert_run(self, agent_name, role, status, logs=None):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO agent_runs (task_id, agent_name, role, status, logs) VALUES (?,?,?,?,?)",
            (self.task_id, agent_name, role, status, json.dumps(logs) if logs is not None else None)
        )
        conn.commit()
        conn.close()

    def test_research_runs_without_subprocess_call(self):
        """RESEARCH uses MemoryResearchInvoker which queries SQLite directly -- no subprocess.

        Even in a fresh DB with no knowledge documents, RESEARCH completes successfully
        with an empty snippets list. This verifies ENGINE does not need any LLM CLI.
        """
        from adapters.sqlite_memory import SQLiteMemoryStore, MemoryResearchInvoker

        store = SQLiteMemoryStore(self.db)
        invoker = MemoryResearchInvoker(store)

        result = invoker.invoke(self.task_id, "RESEARCH", self.db, {})
        self.assertIn("run_id", result)
        self.assertIn("snippets", result)
        self.assertIsInstance(result["snippets"], list)

        # Confirm agent_runs row inserted with agent_name='engine'
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT agent_name, role, status FROM agent_runs WHERE task_id=? AND role='RESEARCH'",
            (self.task_id,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "engine")
        self.assertEqual(row[2], "completed")

    def test_research_finds_relevant_documents(self):
        """RESEARCH with indexed knowledge docs returns relevant snippets."""
        from adapters.sqlite_memory import SQLiteMemoryStore, MemoryResearchInvoker

        store = SQLiteMemoryStore(self.db)
        # Use add_rule() (no file I/O) to insert a knowledge doc directly
        store.add_rule("Python Guide", "Always use type hints in function signatures.")

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET description='How to use type hints in Python' WHERE id=?", (self.task_id,))
        conn.commit()
        conn.close()

        invoker = MemoryResearchInvoker(store)
        result = invoker.invoke(self.task_id, "RESEARCH", self.db, {})
        self.assertTrue(len(result["snippets"]) >= 1)
        titles = [s["title"] for s in result["snippets"]]
        self.assertIn("Python Guide", titles)

    def test_spec_receives_research_bundle_in_payload(self):
        """_auto_resolve_payload for SPEC injects the RESEARCH bundle into the payload."""
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from domain.agent import Role

        class DummyNotifier:
            def notify_hold(self, task_id, role, reason): pass

        store = SQLiteCapabilityStore(self.db)
        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))
        orchestrator = OrchestrationService(self.db, policy, store, DummyNotifier(), {})

        research_payload = {"snippets": [{"id": 1, "title": "Test Rule", "content": "Use type hints", "kind": "rule", "score": -0.5}]}
        self._insert_run("engine", "RESEARCH", "completed", research_payload)

        resolved = orchestrator._auto_resolve_payload(self.task_id, Role.SPEC, {})
        self.assertIn("research", resolved)
        self.assertIsInstance(resolved["research"]["snippets"], list)
        self.assertEqual(resolved["research"]["snippets"][0]["title"], "Test Rule")

    def test_planning_receives_research_and_spec_bundles(self):
        """_auto_resolve_payload for PLANNING includes both RESEARCH and SPEC bundles."""
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from domain.agent import Role

        class DummyNotifier:
            def notify_hold(self, task_id, role, reason): pass

        store = SQLiteCapabilityStore(self.db)
        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))
        orchestrator = OrchestrationService(self.db, policy, store, DummyNotifier(), {})

        research_logs = {"snippets": [{"id": 1, "title": "Test Rule", "content": "content", "kind": "rule", "score": -0.5}]}
        spec_logs = {"acceptance_criteria": ["The code must have type hints", "Tests must pass"]}
        self._insert_run("engine", "RESEARCH", "completed", research_logs)
        self._insert_run("claude", "SPEC", "completed", spec_logs)

        resolved = orchestrator._auto_resolve_payload(self.task_id, Role.PLANNING, {})
        self.assertIn("research", resolved)
        self.assertIn("spec", resolved)
        self.assertEqual(resolved["spec"]["acceptance_criteria"], ["The code must have type hints", "Tests must pass"])

    def test_wisdom_writeback_fires_on_review_approval(self):
        """Wisdom write-back creates exactly one wisdom row on REVIEW approval. Idempotent."""
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from adapters.sqlite_memory import SQLiteMemoryStore

        class DummyNotifier:
            def notify_hold(self, task_id, role, reason): pass

        store = SQLiteCapabilityStore(self.db)
        memory_store = SQLiteMemoryStore(self.db)
        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))
        orchestrator = OrchestrationService(self.db, policy, store, DummyNotifier(), {},
                                            memory_store=memory_store)

        self._insert_run("engine", "RESEARCH", "completed", {"snippets": []})
        self._insert_run("claude", "SPEC", "completed", {"acceptance_criteria": ["works"]})
        self._insert_run("claude", "PLANNING", "completed", {"task_summary": "Build the widget"})
        self._insert_run("antigravity", "EXECUTION", "completed", {"stdout": "done"})
        self._insert_run("claude", "REVIEW", "completed", {"verdict": "approved", "feedback": "Looks great!"})

        orchestrator.next_role(self.task_id)

        conn = sqlite3.connect(self.db)
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_documents WHERE kind='wisdom'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "exactly one wisdom row on approval")

        # Second call must not create a duplicate
        orchestrator.next_role(self.task_id)
        conn = sqlite3.connect(self.db)
        count2 = conn.execute(
            "SELECT COUNT(*) FROM knowledge_documents WHERE kind='wisdom' AND task_id=?",
            (self.task_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count2, 1, "wisdom write-back must be idempotent")

    def test_wisdom_not_written_on_changes_requested(self):
        """Wisdom must NOT be written when REVIEW returns 'changes_requested'."""
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from adapters.sqlite_memory import SQLiteMemoryStore

        class DummyNotifier:
            def notify_hold(self, task_id, role, reason): pass

        store = SQLiteCapabilityStore(self.db)
        memory_store = SQLiteMemoryStore(self.db)
        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))
        orchestrator = OrchestrationService(self.db, policy, store, DummyNotifier(), {},
                                            memory_store=memory_store)

        self._insert_run("engine", "RESEARCH", "completed", {"snippets": []})
        self._insert_run("claude", "SPEC", "completed", {"acceptance_criteria": ["works"]})
        self._insert_run("claude", "PLANNING", "completed", {"task_summary": "Build the widget"})
        self._insert_run("antigravity", "EXECUTION", "completed", {"stdout": "done"})
        self._insert_run("claude", "REVIEW", "completed", {"verdict": "changes_requested", "feedback": "fix it"})

        orchestrator.next_role(self.task_id)

        conn = sqlite3.connect(self.db)
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_documents WHERE kind='wisdom'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "wisdom must not be written on changes_requested")

    def test_verify_command_output_injected_into_review_payload(self):
        """verify_command output appears in the reviewer's payload."""
        from application.orchestration_service import OrchestrationService
        from domain.routing_policy import RoutingPolicyService
        from adapters.sqlite_capability_store import SQLiteCapabilityStore
        from domain.agent import AgentName, Role
        from domain.capability import AgentCapability, QuotaStatus

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE tasks SET verify_command=? WHERE id=?",
            ("python -c \"import sys; print('ok'); sys.exit(0)\"", self.task_id)
        )
        conn.commit()
        conn.close()

        class DummyNotifier:
            def notify_hold(self, task_id, role, reason): pass

        store = SQLiteCapabilityStore(self.db)
        policy = RoutingPolicyService(os.path.join(BASE_DIR, "config", "routing.json"))

        captured_payload = {}

        class CapturingReviewInvoker:
            def invoke(self, task_id, role, db_path, payload):
                captured_payload.update(payload)
                return {"verdict": "approved", "feedback": "good"}

        store.save_capability(AgentName.CLAUDE, AgentCapability(installed=True, quota_status=QuotaStatus.AVAILABLE))

        orchestrator = OrchestrationService(
            self.db, policy, store, DummyNotifier(),
            {AgentName.CLAUDE: CapturingReviewInvoker()}
        )

        self._insert_run("engine", "RESEARCH", "completed", {"snippets": []})
        self._insert_run("claude", "SPEC", "completed", {"acceptance_criteria": ["works"]})
        self._insert_run("claude", "PLANNING", "completed", {"implementation_steps": ["do it"], "task_summary": "x"})
        self._insert_run("antigravity", "EXECUTION", "completed", {"stdout": "done"})

        orchestrator.run_leg(self.task_id, Role.REVIEW, {})

        self.assertIn("verify_exit_code", captured_payload)
        self.assertEqual(captured_payload["verify_exit_code"], 0)
        self.assertIn("ok", captured_payload.get("verify_output", ""))


# -- Phase 1: Memory Store Tests -----------------------------------------------

class TestMemoryStore(unittest.TestCase):
    """Tests for SQLiteMemoryStore — the Phase 1 FTS5 memory layer.

    Uses make_temp_db()/remove_db() for full isolation: each test gets its own
    SQLite file so there's no state bleed across tests.
    """

    def setUp(self):
        self.db = make_temp_db()
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        remove_db(self.db)

    def _write_file(self, filename, content):
        path = os.path.join(self.tmp_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _make_store(self):
        from adapters.sqlite_memory import SQLiteMemoryStore
        # Pass a non-existent config path so it defaults gracefully
        return SQLiteMemoryStore(self.db, config_path=os.path.join(self.tmp_dir, "no_memory.json"))

    def test_ingest_and_search_returns_document(self):
        """Ingesting a file then searching for a word in it returns that document."""
        store = self._make_store()
        path = self._write_file("rule1.md", "# Python Style Guide\n\nAlways use type hints in function signatures.")
        result = store.ingest(path)
        self.assertEqual(result["status"], "created")
        self.assertIsInstance(result["id"], int)

        hits = store.search("type hints")
        self.assertTrue(len(hits) >= 1, "Expected at least one search result")
        titles = [h["title"] for h in hits]
        self.assertIn("Python Style Guide", titles)

    def test_ingest_same_content_twice_is_unchanged(self):
        """Ingesting the same content twice (same content_hash) is a no-op."""
        store = self._make_store()
        path = self._write_file("rule2.md", "# No Duplicates\n\nThis content should not duplicate.")
        r1 = store.ingest(path)
        self.assertEqual(r1["status"], "created")
        doc_id = r1["id"]

        r2 = store.ingest(path)
        self.assertEqual(r2["status"], "unchanged")
        self.assertEqual(r2["id"], doc_id)

        # Confirm only one row in the DB
        with sqlite3.connect(self.db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM knowledge_documents WHERE source_path=?",
                (os.path.abspath(path),)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_ingest_changed_content_updates_existing_row(self):
        """Ingesting changed content at same path updates the row, not inserts a new one."""
        store = self._make_store()
        path = self._write_file("rule3.md", "# Original Content\n\nFirst version.")
        r1 = store.ingest(path)
        self.assertEqual(r1["status"], "created")
        doc_id = r1["id"]

        # Overwrite the file with different content
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Updated Content\n\nSecond version with more detail.")
        r2 = store.ingest(path)
        self.assertEqual(r2["status"], "updated")
        self.assertEqual(r2["id"], doc_id)

        # Still only one row
        with sqlite3.connect(self.db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM knowledge_documents WHERE id=?", (doc_id,)
            ).fetchone()[0]
        self.assertEqual(count, 1)

        # Search for new content finds it; old title no longer matches
        hits = store.search("Second version")
        self.assertTrue(any("Updated Content" in h["title"] for h in hits))

    def test_search_empty_query_returns_empty_list(self):
        """search() with empty/whitespace query returns [], not an error."""
        store = self._make_store()
        self.assertEqual(store.search(""), [])
        self.assertEqual(store.search("   "), [])

    def test_search_no_match_returns_empty_list(self):
        """search() with a query matching nothing returns [], not an error."""
        store = self._make_store()
        path = self._write_file("rule4.md", "# Commit Messages\n\nUse imperative mood.")
        store.ingest(path)
        hits = store.search("xyzzy_nonexistent_token_qqqq")
        self.assertEqual(hits, [])

    def test_search_fts5_special_characters_does_not_raise(self):
        """Query containing FTS5-special characters (-, \", *) does not raise."""
        store = self._make_store()
        path = self._write_file("rule5.md", "# Error Handling\n\nAlways catch exceptions.")
        store.ingest(path)
        # These would crash FTS5 MATCH if not sanitized
        for tricky_query in ['explain the "repo"', '-important', 'a* b+', '"quoted phrase"']:
            try:
                result = store.search(tricky_query)
                self.assertIsInstance(result, list)
            except Exception as e:
                self.fail(f"search() raised unexpectedly for {tricky_query!r}: {e}")

    def test_write_wisdom_creates_exactly_one_wisdom_row(self):
        """write_wisdom() creates exactly one row with kind='wisdom' and the right task_id."""
        # Insert a dummy task first so the FK is valid
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO tasks (title) VALUES ('test task')")
            conn.commit()
        task_id = 1

        store = self._make_store()
        store.write_wisdom(task_id, "The implementation worked well. Tests all passed.")

        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT id, kind, task_id, title FROM knowledge_documents WHERE kind='wisdom'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], task_id)
        self.assertIn(str(task_id), rows[0][3])  # title contains task id

    def test_write_wisdom_is_idempotent(self):
        """Calling write_wisdom twice for the same task_id does not create a duplicate."""
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO tasks (title) VALUES ('dup test')")
            conn.commit()
        task_id = 1

        store = self._make_store()
        store.write_wisdom(task_id, "First write.")
        store.write_wisdom(task_id, "Second write — should not create a new row.")

        with sqlite3.connect(self.db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM knowledge_documents WHERE kind='wisdom' AND task_id=?",
                (task_id,)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_ingest_file_with_no_h1_uses_filename_as_title(self):
        """A file with no # H1 line uses the basename-without-extension as title."""
        store = self._make_store()
        path = self._write_file("my-conventions.md", "Just plain text, no heading.")
        result = store.ingest(path)
        self.assertEqual(result["status"], "created")

        with sqlite3.connect(self.db) as conn:
            title = conn.execute(
                "SELECT title FROM knowledge_documents WHERE id=?", (result["id"],)
            ).fetchone()[0]
        self.assertEqual(title, "my-conventions")

    def test_db_schema_creates_knowledge_tables(self):
        """Fresh DB from make_temp_db() includes knowledge_documents and knowledge_fts."""
        with sqlite3.connect(self.db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','shadow')"
            ).fetchall()}
        self.assertIn("knowledge_documents", tables)
        self.assertIn("knowledge_vectors", tables)
        # knowledge_fts is a virtual table — check it via sqlite_master too
        vtables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' OR type='shadow'"
        ).fetchall()}
        # FTS5 creates shadow tables like knowledge_fts_data, knowledge_fts_idx, etc.
        self.assertTrue(
            any("knowledge_fts" in name for name in vtables),
            "Expected FTS5 shadow tables for knowledge_fts"
        )

# -- Phase 4: MCP Server Tests -------------------------------------------------

class TestMCPServer(unittest.TestCase):
    """Tests for the standalone stdlib-only MCP server (mcp_server.py)."""

    def setUp(self):
        self.db = make_temp_db()
        import mcp_server
        self.old_db_path = mcp_server.DB_PATH
        mcp_server.DB_PATH = self.db
        self.mcp = mcp_server

    def tearDown(self):
        self.mcp.DB_PATH = self.old_db_path
        remove_db(self.db)

    def test_initialize(self):
        captured = []
        def mock_send_response(resp):
            captured.append(resp)

        with patch('mcp_server.send_response', side_effect=mock_send_response):
            self.mcp.dispatch_request({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {}
            })

        self.assertEqual(len(captured), 1)
        resp = captured[0]
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        self.assertIn("protocolVersion", resp["result"])

    def test_tools_list(self):
        captured = []
        def mock_send_response(resp):
            captured.append(resp)

        with patch('mcp_server.send_response', side_effect=mock_send_response):
            self.mcp.dispatch_request({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {}
            })

        self.assertEqual(len(captured), 1)
        resp = captured[0]
        tools = resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        self.assertEqual(tool_names, {"search_knowledge", "get_document", "add_rule"})

    def test_tools_call_flow(self):
        captured = []
        def mock_send_response(resp):
            captured.append(resp)

        # 1. Add rule
        with patch('mcp_server.send_response', side_effect=mock_send_response):
            self.mcp.dispatch_request({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "add_rule",
                    "arguments": {
                        "title": "MCP Test Rule",
                        "content": "This is content for testing MCP server."
                    }
                }
            })

        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0]["result"]["isError"])
        captured.clear()

        # Get doc ID from DB
        conn = sqlite3.connect(self.db)
        doc_id = conn.execute("SELECT id FROM knowledge_documents WHERE title='MCP Test Rule'").fetchone()[0]
        conn.close()

        # 2. Get document
        with patch('mcp_server.send_response', side_effect=mock_send_response):
            self.mcp.dispatch_request({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "get_document",
                    "arguments": {
                        "id": doc_id
                    }
                }
            })
        self.assertEqual(len(captured), 1)
        doc = json.loads(captured[0]["result"]["content"][0]["text"])
        self.assertEqual(doc["title"], "MCP Test Rule")
        captured.clear()

        # 3. Search knowledge
        with patch('mcp_server.send_response', side_effect=mock_send_response):
            self.mcp.dispatch_request({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {
                        "query": "testing MCP"
                    }
                }
            })
        self.assertEqual(len(captured), 1)
        search_res = json.loads(captured[0]["result"]["content"][0]["text"])
        self.assertTrue(len(search_res) >= 1)
        self.assertEqual(search_res[0]["title"], "MCP Test Rule")
        captured.clear()

        # 4. Unknown tool error handling
        with patch('mcp_server.send_response', side_effect=mock_send_response):
            self.mcp.dispatch_request({
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "non_existent_tool",
                    "arguments": {}
                }
            })
        self.assertEqual(len(captured), 1)
        self.assertIn("error", captured[0])
        self.assertEqual(captured[0]["error"]["code"], -32601)


def _encode_mcp_frame(msg: dict) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _read_mcp_frame(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("ascii", errors="replace").rstrip("\r\n")
        if line == "":
            break
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length", 0) or 0)
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


class TestMCPServerRealProtocol(unittest.TestCase):
    """
    Regression test for a real bug found via audit: the first implementation
    of mcp_server.py spoke newline-delimited JSON-RPC, but the actual MCP
    stdio transport requires Content-Length-prefixed framing (identical to
    LSP) - a real MCP client would never successfully parse a response from
    the old implementation. TestMCPServer above only exercises dispatch_request()
    with send_response mocked out, which tests the logic but is structurally
    incapable of catching a wire-framing bug, since it never touches real
    stdin/stdout bytes. This test spawns the actual file as a subprocess and
    talks to it over its real stdio using genuine Content-Length framing, so
    a regression back to newline-delimited (or any other framing mistake)
    would show up as a hang or a JSON parse failure here, not a silent pass.
    """

    def test_initialize_and_tools_list_over_real_stdio(self):
        db_path = make_temp_db()
        proc = None
        try:
            env = dict(os.environ)
            env["OPEN_ENGINE_DB_PATH"] = db_path
            proc = subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, "mcp_server.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            proc.stdin.write(_encode_mcp_frame({
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}
            }))
            proc.stdin.flush()
            init_resp = _read_mcp_frame(proc.stdout)
            self.assertIsNotNone(init_resp, "no response read over real stdio - framing likely broken")
            self.assertEqual(init_resp["id"], 1)
            self.assertIn("protocolVersion", init_resp["result"])

            proc.stdin.write(_encode_mcp_frame({
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
            }))
            proc.stdin.flush()
            list_resp = _read_mcp_frame(proc.stdout)
            self.assertIsNotNone(list_resp)
            tool_names = {t["name"] for t in list_resp["result"]["tools"]}
            self.assertEqual(tool_names, {"search_knowledge", "get_document", "add_rule"})
        finally:
            if proc is not None:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        stream.close()
                    except Exception:
                        pass
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            remove_db(db_path)


# -- Runner ---------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  Open Engine Handoff MVP - Verification Suite")
    print("="*60 + "\n")
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2, failfast=False)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
