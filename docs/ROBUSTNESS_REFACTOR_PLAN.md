# Open Engine — Resilient Multi-Agent Orchestration

Refactor plan: quota-aware, self-healing handoff between Codex, Claude, and Antigravity.

Date: 2026-07-01

---

## 1. Context

Open Engine currently moves a task through three legs — Codex (research) → Claude (planning) → Antigravity (execution) — connected by `WorkBaton` records in SQLite. `server.py` is a single hexagonal-shaped intention that isn't actually hexagonal yet:

- `_run_codex`, `_run_claude`, `_run_antigravity` in `server.py` **import the concrete adapters directly** (`import adapters.claude as claude_mod`, etc.) and call them inline. The HTTP layer, the sequencing logic, and the vendor-specific CLI logic are all one layer.
- `adapters/codex.py` never actually invokes a `codex` binary — it only watches `.codex/` for dropped files. `adapters/claude.py` shells out to `claude.exe` for real. `adapters/antigravity.py` detects the CLI but explicitly declines to invoke it ("not yet implemented") and falls back to a manual file-drop inbox.
- `adapters/probe.py` answers one binary question per tool: **installed or not**. There is no concept of *installed-but-out-of-quota*, no cooldown, and no retry/fallback logic anywhere in the codebase.
- Sequencing today is driven by the human clicking buttons in the UI (`runCodex` → `runClaude` → `runAntigravity` in `src/api.js`). If an agent fails mid-chain, nothing re-routes automatically — the task just sits in `agent_runs.status='failed'` or `'blocked'`.

You've now installed the Antigravity CLI, which removes the "not installed" excuse for that leg and makes it the first tool where a real fallback/quota story matters end-to-end.

## 2. Problem

Two problems, and they compound:

**A. No routing intelligence.** The system has exactly one path (Codex→Claude→Antigravity) and no notion of "this agent can't do its job right now, who else can?" Your requirement is a routing policy, not a bug fix:

- Codex quota exhausted → Claude absorbs research + planning.
- Claude unavailable → Codex's research goes straight to Antigravity (planning step is skipped, not blocked).
- Antigravity unavailable → **HOLD**. It is the only leg that writes/executes; nothing substitutes for it.

**B. No reliable way to detect "quota exhausted" versus "broken" versus "just installed."** I researched all three tools before designing anything, because this is the part that will actually determine whether the feature works:

| Tool | Official quota API? | How exhaustion actually surfaces |
|---|---|---|
| Codex CLI | No scriptable command; `/status` only exists inside an interactive session | HTTP 429 with body `{"error":{"type":"usage_limit_reached",...}}`, or CLI text "You've hit your usage limit. Try again in Xh Ym." Known bug class where `/status`, the banner, and actual errors disagree ("phantom limit," reported against v0.124.0). |
| Claude Code CLI | None. Open feature requests (`claude usage --json`, issues #40395, #40793, #44328) are unresolved as of mid-2026. | Text string "Claude AI usage limit reached, please try again after [time]" in the response. No dedicated exit code has been documented. |
| Antigravity CLI (`agy`) | None found. Quotas are a rolling 5-hour window, tier-dependent (free/Pro/Ultra). | CLI/forum reports describe outright workflow blocks when the window is hit; error text mentions rate limit/quota. `agy` is new enough (per-tool docs from May–June 2026) that flags are still moving — a `--model` flag only landed in v1.0.5. |

Conclusion: **there is no clean, documented signal for any of the three tools.** Every adapter has to classify exhaustion heuristically from exit code + stdout/stderr text, at the moment of invocation, and that classification has a real (nonzero) false-positive/false-negative rate. Any design that assumes a clean "quota API" will be wrong. The plan below treats quota detection as a fallible signal with confidence and evidence, not a boolean.

## 3. Domain model — what are the domains, what is the language?

Two bounded contexts, cleanly separated:

- **Task Handoff** (exists today): `Task`, `WorkBaton`, `WorkStash`, `Artifact`, `ApprovalGate`. Unchanged by this plan.
- **Agent Capability & Routing** (new): the thing this plan actually builds.

Ubiquitous language for the new context:

| Term | Meaning |
|---|---|
| `AgentName` | `codex` \| `claude` \| `antigravity` |
| `Role` | `RESEARCH` \| `PLANNING` \| `EXECUTION` — the job a leg does, independent of which agent does it |
| `AgentCapability` | Snapshot of whether an agent can serve a role right now: installed, authenticated, quota status |
| `QuotaSignal` | `{status, confidence, evidence, retry_after}` — the fallible classification of a single invocation's outcome |
| `RoutingPolicy` | Domain service: given a `Role` and the current capability snapshot, returns the `AgentName` to use, a degraded-mode flag, or `HOLD` |
| `Cooldown` | A time window during which a known-exhausted agent is skipped without re-invoking it |
| `Leg` | One agent's turn on a task (what `agent_runs` rows already represent) |
| `HOLD` | Terminal state: no agent can perform the role; a human must intervene |

Role-capability matrix (this *is* the fallback chain you described, made explicit):

| Role | Primary | Absorbs on failure of primary | Terminal if absorbers also unavailable |
|---|---|---|---|
| RESEARCH | Codex | Claude | HOLD (research becomes empty stash; planning is skipped and Claude does raw research inline) |
| PLANNING | Claude | *(none — skipped, not substituted)* | If Claude is down, PLANNING is marked `skipped_degraded`; the baton passes straight from RESEARCH to EXECUTION with a simpler, unvalidated prompt instead of the strict work-order JSON |
| EXECUTION | Antigravity | *(none)* | HOLD — this matches your note that Antigravity is the worker agent and has no substitute |

This directly encodes your examples: Codex-out → Claude does research+brain work; Claude-out → chain becomes Codex→Antigravity; Antigravity-out → hold.

## 4. Ports & adapters (hexagonal)

Today `server.py` (a driving/primary adapter) reaches straight into concrete CLI adapters. The fix is to put a domain + application layer between them, so the HTTP layer only ever talks to one interface (`OrchestrationService`), and swapping or adding an agent never touches `server.py`.

```
domain/
  agent.py             # AgentName, Role enums
  capability.py        # AgentCapability, QuotaSignal, QuotaStatus (value objects)
  routing_policy.py     # RoutingPolicyService — pure function, the matrix from §3
  errors.py             # AgentUnavailableError, AgentQuotaExhaustedError, AgentAuthError

ports/
  agent_invocation.py    # AgentInvocationPort.invoke(prompt, role) -> AgentResult
  quota_probe.py          # QuotaProbePort.classify(exit_code, stdout, stderr) -> QuotaSignal
  capability_store.py     # CapabilityStorePort.get/save(AgentName) -> AgentCapability
  notifier.py              # NotifierPort.notify_hold(task_id, role, reason)

application/
  orchestration_service.py   # use case: run_leg(task_id, role) — the only thing server.py calls

adapters/                     # existing package, extended not replaced
  codex_cli.py           # NEW: actually invokes `codex exec`, replaces file-drop-only behavior
  claude_cli.py          # refactor of existing claude.py behind AgentInvocationPort
  antigravity_cli.py     # NEW real invocation via `agy -p`, replaces the "not yet implemented" stub
  quota_classifier.py    # ONE shared regex/heuristic module used by all three adapters
  sqlite_capability_store.py
  sse_notifier.py

config/
  routing.yaml           # the matrix in §3, as data — not hardcoded in domain code
```

`server.py` shrinks to: parse HTTP → call `orchestration_service.run_leg(task_id, role)` → serialize result. It stops importing `adapters.codex`/`adapters.claude`/`adapters.antigravity` directly.

### Shared quota classifier (the load-bearing piece)

Because none of the three vendors expose a real API, put all the regex/heuristic knowledge in **one** module instead of three copies, so when a vendor changes their error text you fix it in one place:

```python
# adapters/quota_classifier.py
QUOTA_PATTERNS = [
    r"usage limit reached", r"usage_limit_reached", r"You've hit your usage limit",
    r"rate_limit_exceeded", r"429 Too Many Requests", r"exceeded your current quota",
]

def classify(exit_code: int, stdout: str, stderr: str) -> QuotaSignal:
    text = f"{stdout}\n{stderr}"
    for pat in QUOTA_PATTERNS:
        if re.search(pat, text, re.I):
            return QuotaSignal(status=EXHAUSTED, confidence=0.8,
                                evidence=pat, retry_after=_extract_retry_after(text))
    if exit_code != 0:
        return QuotaSignal(status=UNKNOWN_ERROR, confidence=0.3, evidence=text[:200])
    return QuotaSignal(status=AVAILABLE, confidence=1.0, evidence=None)
```

Confidence is stored, not discarded — see §6 on the "phantom limit" risk.

## 5. Data model changes

`capability_probe` gains columns instead of staying a binary yes/no:

```sql
ALTER TABLE capability_probe ADD COLUMN quota_status TEXT
  CHECK(quota_status IN ('available','exhausted','unknown')) DEFAULT 'unknown';
ALTER TABLE capability_probe ADD COLUMN quota_confidence REAL DEFAULT 1.0;
ALTER TABLE capability_probe ADD COLUMN quota_evidence TEXT;
ALTER TABLE capability_probe ADD COLUMN cooldown_until TIMESTAMP;
```

`agent_runs.status` CHECK constraint gains two values: `'quota_exceeded'`, `'skipped_degraded'` (for the PLANNING-skip case).

New table to make routing decisions auditable — this is a money/communication concern as much as a technical one, since it's the record you'd show someone asking "why did Antigravity not run for six hours":

```sql
CREATE TABLE routing_decisions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       INTEGER REFERENCES tasks(id),
  role          TEXT NOT NULL,
  requested_agent TEXT,
  chosen_agent  TEXT,
  reason        TEXT,          -- 'primary_available' | 'fallback_quota' | 'fallback_unavailable' | 'hold'
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 6. Money and cost controls

Every CLI invocation is a real cost: tokens, wall-clock time, and — per the Codex/Claude issue trackers — some 429s still burn part of the request budget before failing. Two controls follow directly from that:

- **Probe-before-invoke.** Before calling an agent, check `capability_probe.cooldown_until`. If still in cooldown, skip straight to the fallback without spending a call. This is the cheap check that prevents repeatedly paying for a call you already know will fail.
- **Cooldown windows, not permanent bans.** Quota windows are rolling (5h for Codex/Claude/Antigravity Pro tiers, weekly for others). Store `cooldown_until` from the parsed retry-after text where available, else default to a conservative 30-minute cooldown, and re-probe after it expires rather than requiring a manual reset.
- **Confidence-gated overrides.** Because Codex has a documented "phantom limit" bug (status says healthy, invocation says exhausted) and Claude's detection is pure text-matching with no vendor-confirmed exit code, a `quota_confidence < 0.5` classification should not silently trigger an hours-long cooldown — surface it and let a human clear it via a small `/api/capability/reset` endpoint rather than the system guessing wrong for a whole afternoon.

## 7. Human connection

`HOLD` is the one state where the system cannot proceed and a person must act. Reuse the existing `approval_gates` + SSE plumbing rather than inventing new plumbing: on `HOLD`, write an `approval_gates`-style record and broadcast a `hold_declared` SSE event with the task, the role that's stuck, and why — the UI already has a live-log panel (`src/state.js` `pushLog`) that can surface it immediately instead of a silent `blocked` row nobody notices.

## 8. Implementation phasing

1. **Extract, don't rewrite.** Move existing `adapters/claude.py` logic behind `AgentInvocationPort` with zero behavior change; add `domain/`, `ports/`, `application/` as empty-ish scaffolding wired to current behavior. Verify `verify_handoff.py` still passes.
2. **Real invocation for the two stubbed legs.** Implement `codex_cli.py` (`codex exec` non-interactively) and `antigravity_cli.py` (`agy -p`, discovering flags via a cached `agy --help` probe since the CLI is new and versions move fast). Both go through the shared `quota_classifier`.
3. **RoutingPolicyService + OrchestrationService.** Encode the §3 matrix from `config/routing.yaml`; rewire `server.py`'s three `_run_*` handlers into one `_run_leg(task_id, role)` that calls the service.
4. **Cooldown + `routing_decisions` audit table + probe-before-invoke.**
5. **Tests.** Extend `verify_handoff.py` with: quota-exhaustion simulation (monkeypatched subprocess output), cooldown expiry, HOLD-on-antigravity-unavailable, and the degraded PLANNING-skip path end to end.

## 9. Tradeoffs and risks

- **Heuristic detection will misfire occasionally** (confirmed vendor-side bug reports for Codex; no error-code contract at all for Claude or Antigravity). Mitigation is confidence scoring plus a manual reset endpoint — not a promise of perfect detection.
- **This is a maintenance surface, not a one-time build.** Vendor CLI output text can change between releases; centralizing patterns in `quota_classifier.py` is what keeps that maintenance to one file.
- **Antigravity is the least stable integration point right now** — it's the newest CLI of the three, flags are still being added release to release. Treat `agy --help` output as data to re-probe periodically, not a fixed contract.

## Sources

- [Codex CLI Usage & Rate Limits: 5-Hour vs Weekly Windows](https://inventivehq.com/blog/codex-cli-usage-rate-limits)
- [Codex CLI quota / entitlement issue — dashboard vs CLI mismatch (#30041)](https://github.com/openai/codex/issues/30041)
- [Codex CLI exits abruptly on rate_limit_exceeded (#690)](https://github.com/openai/codex/issues/690)
- [Improper 429 near end of 5-hour window / phantom limit (#9135)](https://github.com/openai/codex/issues/9135)
- [Codex does not surface 429 from model providers (#4840)](https://github.com/openai/codex/issues/4840)
- [Fallback chain not triggered on 429 quota errors for openai-codex provider (#24102)](https://github.com/openclaw/openclaw/issues/24102)
- [Add CLI command to check usage and remaining quota — Claude Code (#40395)](https://github.com/anthropics/claude-code/issues/40395)
- [Feature request: `claude usage --json` (#40793)](https://github.com/anthropics/claude-code/issues/40793)
- [Feature request: `claude usage` command (#44328)](https://github.com/anthropics/claude-code/issues/44328)
- [Antigravity Limits with Google AI Ultra — community thread](https://support.google.com/gemini/thread/403203601/antigravity-limits-with-google-ai-ultra-at-100-for-all-models)
- [Google AI Pro/Ultra higher rate limits for Antigravity](https://blog.google/feed/new-antigravity-rate-limits-pro-ultra-subsribers/)
- [Google Antigravity Documentation — plans](https://antigravity.google/docs/plans)
- [Antigravity CLI Cheatsheet — commands, non-interactive mode](https://www.scriptbyai.com/antigravity-cli-cheatsheet/)
- [Antigravity CLI Deep Dive: Google's Go-Based Terminal Agent (May 2026)](https://agentpedia.codes/blog/antigravity-cli-deep-dive)
