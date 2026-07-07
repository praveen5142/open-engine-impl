import shlex
import sqlite3
import json
import time
from typing import Dict, Any, Optional
from contextlib import closing

from domain.agent import AgentName, Role
from domain.routing_policy import RoutingPolicyService, RoutingDecision
from domain.errors import AgentQuotaExhaustedError, AgentUnavailableError
from ports.capability_store import CapabilityStorePort
from ports.agent_invocation import AgentInvocationPort
from ports.notifier import NotifierPort
from domain.capability import QuotaStatus

REVIEW_RETRY_LIMIT = 3


class OrchestrationService:
    def __init__(
        self,
        db_path: str,
        routing_policy: RoutingPolicyService,
        capability_store: CapabilityStorePort,
        notifier: NotifierPort,
        agent_invokers: Dict[AgentName, AgentInvocationPort],
        memory_store=None,  # Optional[SQLiteMemoryStore] — injected for wisdom write-back
    ):
        self.db_path = db_path
        self.routing_policy = routing_policy
        self.capability_store = capability_store
        self.notifier = notifier
        self.agent_invokers = agent_invokers
        self.memory_store = memory_store

    def _auto_resolve_payload(self, task_id: int, role: Role, payload: dict) -> dict:
        """
        Fill in the payload a leg needs when the caller (the auto-chaining
        pipeline) didn't supply it explicitly.

        Phase 2 extension:
          - SPEC: receives the RESEARCH bundle (latest completed engine/RESEARCH run's logs).
          - PLANNING: receives RESEARCH bundle + SPEC (from latest completed claude/SPEC run).
          - EXECUTION: receives RESEARCH bundle + SPEC + work_order (as before, plus spec).
          - REVIEW: unchanged structurally, but verify-command execution is handled in run_leg().
        """
        payload = dict(payload or {})
        with closing(sqlite3.connect(self.db_path)) as conn:

            # Helper: fetch logs from the latest completed run of a given role+agent
            def _latest_logs(agent_name: str, role_name: str) -> dict:
                row = conn.execute(
                    """SELECT logs FROM agent_runs
                       WHERE task_id=? AND agent_name=? AND role=? AND status='completed'
                       ORDER BY id DESC LIMIT 1""",
                    (task_id, agent_name, role_name),
                ).fetchone()
                if row and row[0]:
                    try:
                        return json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        return {}
                return {}

            # RESEARCH bundle used by SPEC, PLANNING, and EXECUTION
            research_logs = _latest_logs("engine", Role.RESEARCH.value)

            if role == Role.SPEC:
                if not payload.get("research"):
                    payload["research"] = research_logs

            elif role == Role.PLANNING:
                if not payload.get("research"):
                    payload["research"] = research_logs
                if not payload.get("spec"):
                    payload["spec"] = _latest_logs("claude", Role.SPEC.value)

            elif role == Role.EXECUTION:
                if not payload.get("research"):
                    payload["research"] = research_logs
                if not payload.get("spec"):
                    payload["spec"] = _latest_logs("claude", Role.SPEC.value)

                if not payload.get("work_order"):
                    row = conn.execute(
                        """SELECT logs FROM agent_runs
                           WHERE task_id=? AND agent_name='claude' AND role='PLANNING' AND status='completed'
                           ORDER BY id DESC LIMIT 1""",
                        (task_id,),
                    ).fetchone()
                    if row and row[0]:
                        try:
                            payload["work_order"] = json.loads(row[0])
                        except (json.JSONDecodeError, TypeError):
                            payload["work_order"] = {}

                # If a later REVIEW asked for changes, fold its feedback into
                # the work order so the next Antigravity pass knows what to fix.
                review_row = conn.execute(
                    """SELECT logs FROM agent_runs
                       WHERE task_id=? AND agent_name='claude' AND role='REVIEW' AND status='completed'
                       ORDER BY id DESC LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if review_row and review_row[0]:
                    try:
                        review = json.loads(review_row[0])
                    except (json.JSONDecodeError, TypeError):
                        review = {}
                    if review.get("verdict") == "changes_requested" and review.get("feedback"):
                        payload["work_order"] = payload.get("work_order") or {}
                        payload["work_order"]["review_feedback"] = review["feedback"]

            elif role == Role.REVIEW:
                if not payload.get("work_order"):
                    row = conn.execute(
                        """SELECT logs FROM agent_runs
                           WHERE task_id=? AND agent_name='claude' AND role='PLANNING' AND status='completed'
                           ORDER BY id DESC LIMIT 1""",
                        (task_id,),
                    ).fetchone()
                    if row and row[0]:
                        try:
                            payload["work_order"] = json.loads(row[0])
                        except (json.JSONDecodeError, TypeError):
                            pass
                if not payload.get("execution_output"):
                    row = conn.execute(
                        """SELECT logs FROM agent_runs
                           WHERE task_id=? AND agent_name='antigravity' AND role='EXECUTION' AND status='completed'
                           ORDER BY id DESC LIMIT 1""",
                        (task_id,),
                    ).fetchone()
                    if row and row[0]:
                        try:
                            payload["execution_output"] = json.loads(row[0])
                        except (json.JSONDecodeError, TypeError):
                            pass

        return payload

    def _run_verify_command(self, task_id: int) -> dict | None:
        """Run the task's verify_command if set.

        Returns {'verify_exit_code': int, 'verify_output': str} or None if not set.
        Called from run_leg() before the REVIEW leg so the reviewer gets an
        objective pass/fail signal alongside its subjective code read.
        """
        import subprocess
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT verify_command, project_path FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        if not row or not row[0]:
            return None

        verify_command, project_dir = row[0], row[1]
        try:
            result = subprocess.run(
                shlex.split(verify_command),
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            combined = (result.stdout or "") + (result.stderr or "")
            return {
                "verify_exit_code": result.returncode,
                "verify_output": combined[-2000:],  # last ~2000 chars
            }
        except subprocess.TimeoutExpired:
            return {"verify_exit_code": -1, "verify_output": "verify_command timed out after 300s"}
        except Exception as e:
            return {"verify_exit_code": -1, "verify_output": f"verify_command error: {e}"}

    def _record_decision(self, task_id: int, role: str, requested: AgentName | None, chosen: AgentName | None, reason: str):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO routing_decisions (task_id, role, requested_agent, chosen_agent, reason) VALUES (?, ?, ?, ?, ?)",
                (task_id, role, requested.value if requested else None, chosen.value if chosen else None, reason)
            )
            conn.commit()

    def run_leg(self, task_id: int, role: Role, payload: dict) -> dict:
        # Determine routing
        decision = self.routing_policy.route(role, self.capability_store.get_capability)

        # Primary was what the routing policy requested first
        policy = self.routing_policy.matrix.get(role.value)
        requested = AgentName(policy["primary"]) if policy and "primary" in policy else None

        self._record_decision(task_id, role.value, requested, decision.agent, decision.reason)

        if decision.hold:
            self.notifier.notify_hold(task_id, role.value, decision.reason)
            return {"status": "blocked", "reason": "HOLD declared", "routing_reason": decision.reason}

        if decision.degraded:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute("UPDATE tasks SET status='active' WHERE id=?", (task_id,))
                cur = conn.execute(
                    "INSERT INTO agent_runs (task_id, agent_name, role, status, logs) VALUES (?, ?, ?, ?, ?)",
                    (task_id, requested.value if requested else "unknown", role.value, "skipped_degraded", json.dumps({"reason": decision.reason}))
                )
                run_id = cur.lastrowid
                conn.commit()
            return {"status": "skipped_degraded", "run_id": run_id}

        # We have a chosen agent - fill in whatever the caller didn't supply
        agent = decision.agent
        invoker = self.agent_invokers.get(agent)
        if not invoker:
            raise ValueError(f"No invoker registered for agent {agent}")

        payload = self._auto_resolve_payload(task_id, role, payload)

        if role == Role.EXECUTION and not payload.get("work_order", {}).get("implementation_steps"):
            # Defense in depth against a bad/empty PLANNING output (e.g. a
            # task with no description): running Antigravity against nothing
            # actionable is guaranteed to produce nothing, and REVIEW would
            # just say so again on every retry - since PLANNING only ever
            # runs once per task, that burns the whole review-retry budget
            # for a foregone conclusion instead of failing fast with a clear
            # reason. ClaudeCLIAdapter._invoke_as_planner already prevents
            # new empty PLANNING runs from being stored as "completed"; this
            # guard also covers existing tasks whose PLANNING run predates
            # that fix. Only fires when a completed PLANNING run actually
            # exists and produced nothing - if there's no PLANNING run at all
            # yet, fall through to the normal invoker (e.g. its own
            # CLI-unavailable hand-off path).
            with closing(sqlite3.connect(self.db_path)) as conn:
                planning_exists = conn.execute(
                    "SELECT 1 FROM agent_runs WHERE task_id=? AND role='PLANNING' AND status='completed' LIMIT 1",
                    (task_id,),
                ).fetchone()
            if planning_exists:
                return {
                    "status": "failed",
                    "agent": agent.value,
                    "reason": "PLANNING produced no usable work order (no implementation_steps) - "
                              "add more detail to the task description and create a new one; "
                              "PLANNING only runs once per task.",
                }

        # Phase 2: for REVIEW, run the verify command (if set) and inject its
        # output into the payload so the reviewer has an objective pass/fail signal.
        if role == Role.REVIEW:
            verify_result = self._run_verify_command(task_id)
            if verify_result:
                payload.update(verify_result)

        try:
            result = invoker.invoke(task_id, role.value, self.db_path, payload)
            # If successful, ensure quota status is AVAILABLE
            cap = self.capability_store.get_capability(agent)
            if cap:
                cap.quota_status = QuotaStatus.AVAILABLE
                cap.cooldown_until = None
                self.capability_store.save_capability(agent, cap)
            return {"status": "completed", "agent": agent.value, "result": result}
        except AgentQuotaExhaustedError as e:
            return {"status": "failed", "agent": agent.value, "reason": "quota_exhausted"}
        except AgentUnavailableError as e:
            self.notifier.notify_hold(task_id, role.value, str(e))
            return {"status": "blocked", "agent": agent.value, "reason": str(e), **e.context}
        except Exception as e:
            return {"status": "failed", "agent": agent.value, "reason": str(e)}

    def next_role(self, task_id: int) -> Optional[Role]:
        """
        Decide what the autonomous pipeline should run next for this task.

        Phase 2: now walks RESEARCH → SPEC → PLANNING → EXECUTION → REVIEW in order.
        RESEARCH and SPEC are checked before PLANNING — same SQL pattern as the
        existing EXECUTION/REVIEW checks. Returns None once the task is done
        (REVIEW approved) or a review-retry loop has exhausted its budget.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            runs = conn.execute(
                "SELECT id, agent_name, role, status, logs FROM agent_runs WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()

        def latest(role_name, status=None):
            matches = [r for r in runs if r[2] == role_name and (status is None or r[3] == status)]
            return matches[-1] if matches else None

        # Phase 2: RESEARCH must come first
        research_done = latest(Role.RESEARCH.value, "completed")
        if not research_done:
            return Role.RESEARCH

        # SPEC must follow RESEARCH
        spec_done = latest(Role.SPEC.value, "completed")
        if not spec_done or spec_done[0] < research_done[0]:
            return Role.SPEC

        # PLANNING must follow SPEC
        planning_done = latest(Role.PLANNING.value, "completed")
        if not planning_done or planning_done[0] < spec_done[0]:
            return Role.PLANNING

        # EXECUTION must follow PLANNING
        execution_done = latest(Role.EXECUTION.value, "completed")
        if not execution_done or execution_done[0] < planning_done[0]:
            return Role.EXECUTION

        # REVIEW must follow EXECUTION
        review_runs = [r for r in runs if r[2] == Role.REVIEW.value and r[3] == "completed" and r[0] > execution_done[0]]
        if not review_runs:
            return Role.REVIEW

        latest_review = review_runs[-1]
        try:
            verdict = (json.loads(latest_review[4] or "{}") or {}).get("verdict")
        except (json.JSONDecodeError, TypeError):
            verdict = None

        if verdict == "changes_requested":
            total_review_cycles = len([r for r in runs if r[2] == Role.REVIEW.value and r[3] == "completed"])
            if total_review_cycles < REVIEW_RETRY_LIMIT:
                return Role.EXECUTION
            self._mark_task_status(task_id, "blocked")
            self._create_review_gate(task_id, latest_review)
            return None

        # REVIEW approved — task is done
        self._mark_task_status(task_id, "completed")
        # Phase 2: wisdom write-back on REVIEW approval (auto-pipeline path).
        # Gate-approval path is in server.py::_post_approval.
        self._write_wisdom_if_not_done(task_id, runs, latest_review)
        return None

    def _write_wisdom_if_not_done(self, task_id: int, runs: list, latest_review_row) -> None:
        """Write a wisdom document for a completed task if one doesn't exist yet.

        Dedup guard: checks for an existing wisdom document before writing.
        Composes summary from task title, work order task_summary, review feedback.
        """
        if self.memory_store is None:
            return
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                # Dedup check
                existing = conn.execute(
                    "SELECT id FROM knowledge_documents WHERE kind='wisdom' AND task_id=?",
                    (task_id,)
                ).fetchone()
                if existing:
                    return

                task_row = conn.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
                task_title = task_row[0] if task_row else f"Task #{task_id}"

            # Build summary from available runs
            def _logs(role_name, agent_name):
                matches = [r for r in runs if r[2] == role_name and r[1] == agent_name and r[3] == "completed"]
                if not matches:
                    return {}
                try:
                    return json.loads(matches[-1][4] or "{}") or {}
                except (json.JSONDecodeError, TypeError):
                    return {}

            planning_logs = _logs(Role.PLANNING.value, "claude")
            review_logs = {}
            try:
                review_logs = json.loads(latest_review_row[4] or "{}") or {}
            except (json.JSONDecodeError, TypeError):
                pass

            task_summary = planning_logs.get("task_summary", "")
            feedback = review_logs.get("feedback", "")

            summary_parts = [f"Task: {task_title}"]
            if task_summary:
                summary_parts.append(f"Work order summary: {task_summary}")
            if feedback:
                summary_parts.append(f"Review feedback: {feedback}")
            summary_parts.append("Outcome: approved by REVIEW stage.")

            summary = "\n".join(summary_parts)
            self.memory_store.write_wisdom(task_id, summary)
        except Exception:
            pass  # wisdom write-back must never crash the pipeline

    def _mark_task_status(self, task_id: int, status: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
            conn.commit()

    def _create_review_gate(self, task_id: int, latest_review_row) -> None:
        """
        REVIEW retries are exhausted and the task is now 'blocked', but
        without this there is nothing anywhere in the UI a human can actually
        click to resolve it - the Approval Gates panel already exists for
        exactly this ("a human needs to make a call"), so surface it there
        instead of inventing a parallel mechanism. Guarded against duplicates
        so re-clicking 'Run Task' on an already-blocked task doesn't spawn a
        second gate for the same review run.
        """
        run_id, _, _, _, logs = latest_review_row
        try:
            review = json.loads(logs or "{}")
        except (json.JSONDecodeError, TypeError):
            review = {}

        with closing(sqlite3.connect(self.db_path)) as conn:
            existing = conn.execute(
                "SELECT id FROM approval_gates WHERE task_id=? AND agent_run_id=? AND action_type='review_decision'",
                (task_id, run_id),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO approval_gates (task_id, agent_run_id, action_type, payload, status) VALUES (?,?,?,?,?)",
                    (
                        task_id,
                        run_id,
                        "review_decision",
                        json.dumps({
                            "message": f"Claude's review requested changes {REVIEW_RETRY_LIMIT} times in a row. "
                                       "Approve to accept the current implementation as done, or reject to leave "
                                       "this task blocked for manual follow-up.",
                            "feedback": review.get("feedback", ""),
                        }),
                        "pending",
                    ),
                )
                conn.commit()
