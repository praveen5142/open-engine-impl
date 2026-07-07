import subprocess
import json
import re
import shutil
import sqlite3
import os
import textwrap

from ports.agent_invocation import AgentInvocationPort
from domain.agent import AgentName
from domain.errors import AgentQuotaExhaustedError, AgentUnavailableError
from adapters.quota_classifier import RegexQuotaClassifier
from ports.capability_store import CapabilityStorePort
from domain.capability import QuotaStatus

EXECUTION_TIMEOUT_SECONDS = 900


def _find_claude_cli() -> str | None:
    """Resolve the `claude` binary via PATH so this works on any machine it's
    installed on, not just the one it was originally developed on (this used
    to be a hardcoded absolute path tied to one developer's home directory)."""
    for name in ("claude", "claude.exe", "claude.cmd"):
        path = shutil.which(name)
        if path:
            return path
    return None

class ClaudeCLIAdapter(AgentInvocationPort):
    """
    Claude fills two roles in the pipeline:

    - PLANNING: read the task directly and produce a structured work order JSON.
    - REVIEW: given the PLANNING work order and what Antigravity produced,
      judge whether the implementation satisfies the work order and either
      approve or request changes (which routes the task back to EXECUTION).
    """

    def __init__(self, classifier: RegexQuotaClassifier, store: CapabilityStorePort):
        self.classifier = classifier
        self.store = store

    def invoke(self, task_id: int, role: str, db_path: str, payload: dict) -> dict:
        if role == "REVIEW":
            return self._invoke_as_reviewer(task_id, db_path, payload)
        if role == "SPEC":
            return self._invoke_as_spec(task_id, db_path, payload)
        return self._invoke_as_planner(task_id, db_path, payload)

    def _run_claude_cli(self, prompt: str, project_dir: str | None = None):
        """Shared subprocess + quota-classification plumbing for both roles.

        `project_dir` gets passed as `--add-dir` for both roles. REVIEW needs
        it to read the actual files Antigravity changed, not just a stdout
        summary. PLANNING needs it too - found via a real run against a task
        titled "Repo Explaination" for an unrelated project: without
        --add-dir, Claude has nothing to ground a plan in beyond the task's
        title/description, so for any "explain/describe the existing repo"
        style task it silently fell back to whatever's on its own default
        working directory (this server's own cwd, i.e. this open-engine repo)
        instead of the actual active project - producing a work order that
        confidently described the wrong codebase. Not every PLANNING task
        needs to read files (a brand-new feature has no existing code to
        read), but there's no way to know that in advance, so always giving
        it the option is strictly safer than never giving it the option.
        """
        claude_path = _find_claude_cli()
        if not claude_path:
            raise AgentUnavailableError("Claude CLI ('claude') not found on PATH")

        args = [
            claude_path,
            "-p",
            "--permission-mode", "plan",
            "--output-format", "json",
            "--no-session-persistence",
        ]
        if project_dir:
            args += ["--add-dir", project_dir]
        try:
            result = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=EXECUTION_TIMEOUT_SECONDS,
                # Verified live: passing --add-dir alone was NOT enough to
                # redirect Claude away from the wrong repo. Claude CLI treats
                # its OS-level cwd (inherited from this server process, i.e.
                # wherever server.py itself was launched from) as the primary
                # project, and --add-dir only grants supplementary access to
                # an extra directory on top of that - it does not replace it.
                # Every prior test in this session happened to have the
                # active project be the same directory server.py runs from,
                # so this never surfaced until a task targeted a genuinely
                # different project. Setting cwd explicitly makes the target
                # project Claude's actual primary context.
                cwd=project_dir or None,
            )
        except FileNotFoundError:
            # Clean "unavailable" signal instead of leaking a raw OS errno
            # string up through the API - matches how antigravity_cli.py
            # reports a missing CLI.
            raise AgentUnavailableError(f"Claude CLI not found at {claude_path}")

        signal = self.classifier.classify(result.returncode, result.stdout, result.stderr)
        cap = self.store.get_capability(AgentName.CLAUDE)
        if cap:
            cap.quota_status = signal.status
            if signal.status == QuotaStatus.EXHAUSTED:
                cap.cooldown_until = signal.retry_after
            self.store.save_capability(AgentName.CLAUDE, cap)

        if signal.status == QuotaStatus.EXHAUSTED:
            raise AgentQuotaExhaustedError("Claude usage limit reached")
        if result.returncode != 0:
            raise RuntimeError(f"Claude failed: {result.stderr}")
        return result.stdout.strip()

    def _parse_claude_json(self, raw: str):
        """Unwrap `claude -p --output-format json`'s outer envelope
        ({"result": "<json-or-text>"}) and extract the JSON payload from the
        assistant's actual text.

        Found via a real end-to-end pipeline run: REVIEW explains its
        reasoning before giving a verdict, so the assistant text is prose
        ending in a ```json fenced block, not pure JSON. The old
        implementation only handled pure-JSON text; on prose it fell back to
        re-scanning the *outer* envelope string (which starts with its own
        unrelated '{'), silently recovering the envelope itself (no
        "verdict" key) instead of the assistant's real answer - every REVIEW
        was misclassified as changes_requested even when Claude said
        "approved", burning the whole retry budget for nothing. This tries
        progressively looser strategies against the actual assistant text.
        """
        try:
            outer = json.loads(raw)
            inner = outer.get("result") or outer.get("content") or raw
        except json.JSONDecodeError:
            inner = raw

        if not isinstance(inner, str):
            return inner

        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass

        fenced = re.search(r"```json\s*(\{.*?\})\s*```", inner, re.DOTALL)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except json.JSONDecodeError:
                pass

        start, end = inner.rfind("{"), inner.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(inner[start:end + 1])
            except json.JSONDecodeError:
                pass

        return {}

    def _active_project_dir(self, db_path: str) -> str:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT path FROM active_project WHERE id=1").fetchone()
        conn.close()
        return row[0] if row else os.getcwd()

    # -- PLANNING --------------------------------------------------------------

    def _invoke_as_planner(self, task_id: int, db_path: str, payload: dict) -> dict:
        conn = sqlite3.connect(db_path)
        task_row = conn.execute("SELECT title, description FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
        if not task_row:
            raise ValueError(f"Task {task_id} not found")
        title, description = task_row

        project_dir = self._active_project_dir(db_path)

        # Phase 2: include research bundle and spec if available
        research = payload.get("research", {})
        spec = payload.get("spec", {})

        research_section = ""
        if research and research.get("snippets"):
            snippets = research["snippets"]
            research_section = "\n\n## Research Context (retrieved from memory)\n"
            for s in snippets[:5]:
                research_section += f"### {s.get('title', 'Snippet')}\n{s.get('content', '')[:500]}\n\n"

        spec_section = ""
        if spec and spec.get("acceptance_criteria"):
            spec_section = f"\n\n## Acceptance Criteria (from SPEC)\n{json.dumps(spec, indent=2)}"

        prompt = textwrap.dedent(f"""
        You are Claude Code operating in PLAN-ONLY mode. Your job is to read the
        task below and produce a structured work order as valid JSON with keys:
        work_order_id, task_summary, implementation_steps (array of strings),
        risk_assessment ({{level, notes}}), recommended_action.

        The project directory is available to you via --add-dir - if the task
        requires understanding existing code (e.g. explaining, summarizing, or
        modifying something that already exists), actually read the relevant
        files before writing the work order instead of guessing. If this is a
        brand-new feature with nothing to read yet, plan from the task
        description alone.

        ## Task
        Title      : {title}
        Description: {description}
        {research_section}{spec_section}
        """).strip()

        raw = self._run_claude_cli(prompt, project_dir=project_dir)
        work_order = self._parse_claude_json(raw)

        # Found via a real run against a task with no description ("hello
        # world", title only): Claude sometimes doesn't return a structured
        # work order at all for a too-sparse task, _parse_claude_json
        # correctly returns {} (there's genuinely no JSON to find), and the
        # old code stored that {} as a "completed" PLANNING run anyway. Since
        # PLANNING only ever runs once per task, every later EXECUTION retry
        # kept re-feeding Antigravity the same empty work order forever -
        # REVIEW correctly said "nothing to review" 3 times in a row before
        # the task could ever reach a terminal state. Fail loudly here
        # instead of silently reporting success with nothing usable.
        if not work_order or not work_order.get("implementation_steps"):
            print(f"[claude_cli] PLANNING for task {task_id} produced no usable work order. Raw output:\n{raw}")
            raise RuntimeError(
                "Claude did not return a usable work order (no implementation_steps) - "
                "the task description is probably too sparse to plan from. Add more "
                "detail to the task and create a new one (PLANNING only runs once per task)."
            )

        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "INSERT INTO agent_runs (task_id, agent_name, role, status, started_at, completed_at, logs) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
            (task_id, "claude", "PLANNING", "completed", json.dumps(work_order))
        )
        run_id = cur.lastrowid
        conn.commit()
        conn.close()

        return work_order

    # -- REVIEW ------------------------------------------------------------------

    def _invoke_as_reviewer(self, task_id: int, db_path: str, payload: dict) -> dict:
        work_order = payload.get("work_order", {})
        execution_output = payload.get("execution_output", {})
        project_dir = self._active_project_dir(db_path)

        # Phase 2: include verify_command output if present
        verify_section = ""
        verify_exit_code = payload.get("verify_exit_code")
        verify_output = payload.get("verify_output")
        if verify_exit_code is not None and verify_output is not None:
            status = "PASSED" if verify_exit_code == 0 else f"FAILED (exit code {verify_exit_code})"
            verify_section = f"\n\n## Verify Command Output\nStatus: {status}\n```\n{verify_output}\n```"

        prompt = textwrap.dedent(f"""
        You are Claude Code operating as REVIEWER in an automated handoff
        pipeline. Antigravity just implemented the work order below directly
        in this project's files (available to you via --add-dir). Inspect the
        actual code - do not just trust the summary - and decide whether the
        implementation satisfies the work order.

        Respond with valid JSON only: {{"verdict": "approved" or
        "changes_requested", "feedback": "<specific, actionable feedback for
        another implementation pass, or a short approval note>"}}.

        ## Work Order
        {json.dumps(work_order, indent=2)}

        ## Antigravity's Reported Output
        {json.dumps(execution_output, indent=2)}{verify_section}
        """).strip()

        raw = self._run_claude_cli(prompt, project_dir=project_dir)
        review = self._parse_claude_json(raw)
        if review.get("verdict") not in ("approved", "changes_requested"):
            review["verdict"] = "changes_requested"
            review.setdefault("feedback", "Reviewer did not return a valid verdict; treating as changes requested.")

        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "INSERT INTO agent_runs (task_id, agent_name, role, status, started_at, completed_at, logs) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
            (task_id, "claude", "REVIEW", "completed", json.dumps(review))
        )
        run_id = cur.lastrowid
        conn.commit()
        conn.close()

        return review

    # -- SPEC (Phase 2) ----------------------------------------------------------

    def _invoke_as_spec(self, task_id: int, db_path: str, payload: dict) -> dict:
        """Produce a structured acceptance-criteria spec grounded in research.

        Structural template: same shape as _invoke_as_planner (prompt → CLI →
        parse → validate → insert agent_runs → return).

        Validates that acceptance_criteria is non-empty: an empty/unusable spec
        must fail loudly (not be recorded as 'completed'), mirroring the
        implementation_steps guard in _invoke_as_planner.
        """
        conn = sqlite3.connect(db_path)
        task_row = conn.execute("SELECT title, description FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
        if not task_row:
            raise ValueError(f"Task {task_id} not found")
        title, description = task_row

        project_dir = self._active_project_dir(db_path)
        research = payload.get("research", {})

        research_section = ""
        if research and research.get("snippets"):
            snippets = research["snippets"]
            research_section = "\n\n## Research Context (from memory store)\n"
            for s in snippets[:5]:
                research_section += f"### {s.get('title', 'Snippet')}\n{s.get('content', '')[:500]}\n\n"

        prompt = textwrap.dedent(f"""
        You are Claude Code operating as SPEC WRITER in an automated development
        pipeline. Your job is to author a concise acceptance-criteria spec for
        the task below, grounded in the research context from the project's
        memory store.

        Respond with valid JSON only, with keys:
          spec_id       (a short slug, e.g. "spec-task-{task_id}")
          objective     (one sentence describing what this task achieves)
          acceptance_criteria  (array of strings — each is a concrete, testable criterion)
          out_of_scope  (array of strings — what this task explicitly does NOT do)
          files_expected (array of strings — file paths likely to be created/modified)

        ## Task
        Title      : {title}
        Description: {description}
        {research_section}
        """).strip()

        raw = self._run_claude_cli(prompt, project_dir=project_dir)
        spec = self._parse_claude_json(raw)

        # Validate: acceptance_criteria must be non-empty (mirrors PLANNING's guard)
        if not spec or not spec.get("acceptance_criteria"):
            print(f"[claude_cli] SPEC for task {task_id} produced no acceptance_criteria. Raw output:\n{raw}")
            raise RuntimeError(
                "Claude did not return a usable spec (no acceptance_criteria) - "
                "the task description is probably too sparse. Add more detail to the "
                "task and create a new one."
            )

        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "INSERT INTO agent_runs (task_id, agent_name, role, status, started_at, completed_at, logs) "
            "VALUES (?, 'claude', 'SPEC', 'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
            (task_id, json.dumps(spec))
        )
        conn.commit()
        conn.close()

        return spec
