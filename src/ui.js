/**
 * src/ui.js  –  Component renderer
 * Subscribes to State and re-renders each frame on every change.
 */
import State from './state.js';
import * as API from './api.js';

// ── Utility helpers ───────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }

function statusBadge(status) {
  const map = {
    pending:   ['badge--blocked', 'pending'],
    active:    ['badge--warn',    'active'],
    running:   ['badge--warn',    'running'],
    completed: ['badge--ok',      'done'],
    failed:    ['badge--err',     'failed'],
    blocked:   ['badge--err',     'blocked'],
  };
  const [cls, label] = map[status] || ['badge--blocked', status];
  return `<span class="badge ${cls}">${label}</span>`;
}

function agentBadge(agent) {
  const map = { codex: 'badge--codex', claude: 'badge--claude', antigravity: 'badge--agy', engine: 'badge--engine' };
  return `<span class="badge ${map[agent]||'badge--blocked'}">${agent}</span>`;
}

function timeAgo(ts) {
  if (!ts) return '';
  // SQLite's CURRENT_TIMESTAMP produces "YYYY-MM-DD HH:MM:SS" in UTC with no
  // timezone marker. The JS Date parser treats that exact shape as *local*
  // time instead, so every timestamp from the backend (task/run/artifact/
  // gate created_at, started_at, etc.) silently drifted by the browser's UTC
  // offset - "6h ago" instead of the real elapsed time, in either direction
  // depending on timezone. Force it to parse as UTC.
  const iso = typeof ts === 'string' && ts.includes(' ') && !ts.includes('T')
    ? ts.replace(' ', 'T') + 'Z'
    : ts;
  const diff = (Date.now() - new Date(iso)) / 1000;
  if (diff < 60)   return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff/60)}m ago`;
  return `${Math.round(diff/3600)}h ago`;
}

function syntaxHighlight(obj) {
  if (!obj) return 'null';
  let json = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
  return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, (match) => {
    let cls = 'json-num';
    if (/^"/.test(match)) cls = /:$/.test(match) ? 'json-key' : 'json-str';
    else if (/true|false/.test(match)) cls = 'json-bool';
    else if (/null/.test(match)) cls = 'json-null';
    return `<span class="${cls}">${match}</span>`;
  });
}

// ── Frame 1: Command Center ───────────────────────────────────────────────────

function renderCommandCenter(state) {
  // Task list
  const listEl = el('task-list');
  if (!listEl) return;
  if (!state.tasks.length) {
    listEl.innerHTML = `<div class="empty">
      <div class="empty-icon">📋</div>
      <div class="empty-label">No tasks yet.<br>Create your first handoff task.</div>
    </div>`;
    return;
  }
  listEl.innerHTML = state.tasks.map(t => `
    <div class="task-item ${t.id === state.activeTaskId ? 'active' : ''} fade-in"
         data-task-id="${t.id}">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <span class="task-item-title">${escHtml(t.title)}</span>
        ${statusBadge(t.status)}
      </div>
      <div class="task-item-meta">#${t.id} · ${timeAgo(t.created_at)}</div>
    </div>`).join('');

  listEl.querySelectorAll('.task-item').forEach(item => {
    item.addEventListener('click', () => {
      const tid = parseInt(item.dataset.taskId, 10);
      State.set({ activeTaskId: tid });
      API.loadTask(tid);
    });
  });
}

// ── Frame 2: Focus Mode ───────────────────────────────────────────────────────

function renderDelegateBar(state) {
  const bar = el('delegate-bar');
  if (!bar) return;
  const task = State.activeTask();
  if (!task) { bar.innerHTML = ''; return; }
  bar.innerHTML = `
    <button id="btn-delegate" class="btn btn--primary btn--sm">▶ Run Task (Delegate Routing)</button>
    <span style="font-size:11px;color:#94a3b8">plans with Claude, implements with Antigravity, reviews with Claude — fully autonomous</span>`;
  el('btn-delegate')?.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try { await API.runTask(task.id); }
    finally { btn.disabled = false; }
  });
}

function renderFocusMode(state) {
  const task = state.activeTask ? state.activeTask() : State.activeTask();
  const stepperEl = el('stepper');
  const focusEl   = el('focus-panel');
  if (!stepperEl || !focusEl) return;

  if (!task) {
    stepperEl.innerHTML = '';
    focusEl.innerHTML = `<div class="empty"><div class="empty-icon">👈</div><div class="empty-label">Select or create a task to start.</div></div>`;
    return;
  }

  const REVIEW_RETRY_LIMIT = 3;
  const runs = state.agentRuns[task.id] || [];
  const runByRole = (role) => runs.filter(r => r.role === role).slice(-1)[0];

  const researchRun = runByRole('RESEARCH');
  const specRun = runByRole('SPEC');
  const planRun = runByRole('PLANNING');
  const execRuns = runs.filter(r => r.role === 'EXECUTION');
  const execRun = execRuns.slice(-1)[0];
  const completedExecCount = execRuns.filter(r => r.status === 'completed').length;
  const reviewRuns = runs.filter(r => r.role === 'REVIEW');
  const reviewRun = reviewRuns.slice(-1)[0];
  const completedReviewCount = reviewRuns.filter(r => r.status === 'completed').length;
  const retryPill = (count) => count > 1 ? `<span class="badge badge--warn" style="margin-left:4px;padding:1px 5px;font-size:9px">×${count}</span>` : '';

  let reviewVerdict = null;
  if (reviewRun?.status === 'completed') {
    try { reviewVerdict = JSON.parse(reviewRun.logs || '{}'); } catch (e) { reviewVerdict = null; }
  }

  function stepClass(run) {
    if (!run) return '';
    if (run.status === 'completed') return 'done';
    if (run.status === 'running')   return 'active';
    if (run.status === 'blocked' || run.status === 'failed') return 'blocked';
    return 'active';
  }

  stepperEl.innerHTML = `
    <div class="step ${stepClass(researchRun)}">
      <div class="step-dot">🔍</div>
      <span class="step-label">Research (Engine)</span>
    </div>
    <div class="step ${stepClass(specRun)}">
      <div class="step-dot">📄</div>
      <span class="step-label">Spec (Claude)</span>
    </div>
    <div class="step ${stepClass(planRun)}">
      <div class="step-dot">◆</div>
      <span class="step-label">Plan (Claude)</span>
    </div>
    <div class="step ${stepClass(execRun)}">
      <div class="step-dot">✦</div>
      <span class="step-label">Execute (Antigravity)${retryPill(completedExecCount)}</span>
    </div>
    <div class="step ${stepClass(reviewRun)}">
      <div class="step-dot">◆</div>
      <span class="step-label">Review (Claude)${retryPill(completedReviewCount)}</span>
    </div>`;

  let actionArea = '';
  const activeRun = [reviewRun, execRun, planRun, specRun, researchRun].find(r => r?.status === 'running');
  const blockedExec = execRun?.status === 'blocked';
  const changesRequested = reviewVerdict?.verdict === 'changes_requested';
  const approved = reviewVerdict?.verdict === 'approved';

  if (task.status === 'completed') {
    let label = 'reviewed & approved';
    if (reviewVerdict?.verdict === 'changes_requested') {
      label = `manually approved after ${completedReviewCount} review cycle(s)`;
    }
    actionArea = `<div style="display:flex;align-items:center;gap:10px;padding:12px;background:#052e16;border:1px solid #6ee7b7;border-radius:8px">
      <span style="font-size:20px">✅</span>
      <div>
        <div style="font-size:13px;font-weight:600;color:#6ee7b7">Task complete — ${label}</div>
        ${reviewVerdict?.feedback ? `<div style="font-size:12px;color:#34d399;margin-top:2px">${escHtml(reviewVerdict.feedback)}</div>` : ''}
      </div>
    </div>`;
  } else if (task.status === 'blocked') {
    if (state.pendingApprovals.some(g => g.task_id === task.id && g.action_type === 'review_decision')) {
      actionArea = `<div style="display:flex;flex-direction:column;gap:8px;padding:12px;background:#1f0707;border:1px solid #f87171;border-radius:8px">
        <div style="font-size:13px;font-weight:600;color:#fca5a5">Needs a human — review kept requesting changes after ${REVIEW_RETRY_LIMIT} attempts</div>
        <div style="font-size:12px;color:#fca5a5">${escHtml(reviewVerdict?.feedback || '')}</div>
        <div style="font-size:12px;color:#cbd5e1">Resolve this in the <b>Approval Gates</b> panel (Frame 3, right) — Approve to accept the implementation as-is, or Reject to leave it blocked for manual follow-up.</div>
      </div>`;
    } else {
      actionArea = `<div style="display:flex;align-items:center;gap:10px;padding:12px;background:#1f0707;border:1px solid #f87171;border-radius:8px">
        <span class="badge badge--err">BLOCKED</span>
        <div>
          <div style="font-size:13px;font-weight:600;color:#fca5a5">Blocked — see Audit Trail for the last agent run's status</div>
        </div>
      </div>`;
    }
  } else if (blockedExec) {
    let blockInfo = '';
    try { blockInfo = JSON.parse(execRun.logs); } catch(e) { blockInfo = { blocked_reason: execRun.logs }; }
    actionArea = `
      <div style="display:flex;flex-direction:column;gap:8px">
        <div style="display:flex;align-items:center;gap:8px">
          <span class="badge badge--err">BLOCKED</span>
          <span style="font-size:12px;color:#cbd5e1">Antigravity awaiting manual input</span>
        </div>
        <div style="font-size:12px;color:#94a3b8;background:#1c1508;padding:10px;border-radius:6px;border:1px solid #fde68a">
          ${escHtml(blockInfo.blocked_reason || execRun.logs || '')}
        </div>
        <div style="font-size:12px;color:#cbd5e1;font-weight:500">Drop return artifact to handoff-lab/ then click poll:</div>
        <div class="focus-actions">
          <button id="btn-poll-agy" class="btn btn--ghost btn--sm">🔄 Poll for Return Artifacts</button>
        </div>
      </div>`;
  } else if (changesRequested) {
    actionArea = `<div style="display:flex;flex-direction:column;gap:8px;padding:12px;background:#1c1508;border:1px solid #fde68a;border-radius:8px">
      <div style="font-size:13px;font-weight:600;color:#fbbf24">Changes requested — re-executing (attempt ${completedReviewCount + 1}/${REVIEW_RETRY_LIMIT})…</div>
      <div style="font-size:12px;color:#cbd5e1">${escHtml(reviewVerdict.feedback || '')}</div>
    </div>`;
  } else if (runs.some(r => r.status === 'failed')) {
    const failedRun = runs.find(r => r.status === 'failed');
    actionArea = `
      <div style="display:flex;align-items:center;gap:10px;padding:12px;background:#1f0707;border:1px solid #f87171;border-radius:8px">
        <span class="badge badge--err">FAILED</span>
        <div>
          <div style="font-size:13px;font-weight:600;color:#fca5a5">${failedRun.agent_name} (${failedRun.role || '?'}) leg failed</div>
          <div style="font-size:12px;color:#fca5a5;margin-top:2px">${escHtml(failedRun.logs || 'Check server logs for details.')}</div>
        </div>
      </div>`;
  } else if (activeRun) {
    actionArea = `<div style="display:flex;align-items:center;gap:10px">
      <div class="spinner"></div>
      <span style="font-size:13px;color:#cbd5e1">${activeRun.role || activeRun.agent_name} is running…</span>
    </div>`;
  } else {
    actionArea = `<div style="font-size:13px;color:#94a3b8">Click "Run Task" above to start the autonomous pipeline.</div>`;
  }

  focusEl.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <div>
        <div style="font-size:15px;font-weight:700;color:#e2e8f0">${escHtml(task.title)}</div>
        <div style="font-size:12px;color:#94a3b8;margin-top:2px">#${task.id} · ${statusBadge(task.status)}${task.verify_command ? ` · <code style="font-family:var(--txt-mono);font-size:11px;color:#cbd5e1;background:var(--clr-surface);padding:1px 4px;border-radius:3px">${escHtml(task.verify_command)}</code>` : ''}</div>
      </div>
    </div>
    ${actionArea}
    <div class="live-log-wrap">
      <div style="font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Live Log</div>
      <div class="live-log" id="live-log">
        ${state.liveLog.map(e => `<div class="log-line ${e.cls}">${escHtml(e.text)}</div>`).join('')}
      </div>
    </div>`;

  // Scroll log to bottom
  const logEl = el('live-log');
  if (logEl) logEl.scrollTop = logEl.scrollHeight;

  // Wire up action buttons
  el('btn-poll-agy')?.addEventListener('click', async () => {
    await API.pollAntigravity(task.id);
  });
}

// ── Frame 3: Atomic Review ────────────────────────────────────────────────────

function renderAtomicReview(state) {
  const task = State.activeTask();

  // Approval gates
  const gatesEl = el('approval-gates-body');
  if (gatesEl) {
    const gates = state.pendingApprovals.filter(g => !task || g.task_id === task.id);
    if (!gates.length) {
      gatesEl.innerHTML = `<div style="font-size:12px;color:#94a3b8;padding:8px 0">No pending approvals</div>`;
    } else {
      gatesEl.innerHTML = gates.map(g => {
        let payload = {};
        try { payload = JSON.parse(g.payload); } catch(e){}
        const body = g.action_type === 'review_decision'
          ? `<div style="font-size:12px;color:#cbd5e1;margin-top:4px">${escHtml(payload.message || '')}</div>
             ${payload.feedback ? `<div style="font-size:11px;color:#94a3b8;background:#1c1508;padding:8px;border-radius:6px;margin-top:6px;border:1px solid #fde68a">${escHtml(payload.feedback)}</div>` : ''}`
          : `<div style="font-family:var(--txt-mono);font-size:11px;color:#cbd5e1">${escHtml(JSON.stringify(payload))}</div>`;
        const label = g.action_type === 'review_decision' ? 'Needs a Human — Review Stuck' : g.action_type;
        return `<div class="approval-gate" data-gate-id="${g.id}">
          <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#fbbf24">${escHtml(label)}</div>
          ${body}
          <div class="approval-actions">
            <button class="btn btn--ok btn--sm btn-approve" data-id="${g.id}">Approve</button>
            <button class="btn btn--err btn--sm btn-reject" data-id="${g.id}">Reject</button>
          </div>
        </div>`;
      }).join('');
      gatesEl.querySelectorAll('.btn-approve').forEach(b =>
        b.addEventListener('click', () => API.approveGate(parseInt(b.dataset.id)))
      );
      gatesEl.querySelectorAll('.btn-reject').forEach(b =>
        b.addEventListener('click', () => API.rejectGate(parseInt(b.dataset.id)))
      );
    }
  }

  // Research Context
  const researchBody = el('research-context-body');
  if (researchBody && task) {
    const runs = state.agentRuns[task.id] || [];
    const researchRun = runs.filter(r => r.agent_name === 'engine' && r.role === 'RESEARCH' && r.status === 'completed').slice(-1)[0];
    if (researchRun?.logs) {
      try {
        const resObj = JSON.parse(researchRun.logs);
        const snippets = resObj.snippets || [];
        if (!snippets.length) {
          researchBody.innerHTML = `<div style="font-size:12px;color:#94a3b8;padding:8px 0">No relevant memory snippets found.</div>`;
        } else {
          researchBody.innerHTML = snippets.map(s => `
            <div style="margin-bottom:8px;border:1px solid var(--clr-border);border-radius:var(--r-sm);padding:8px;background:var(--clr-surface)">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px">
                <span style="font-weight:600;font-size:12px;color:#e2e8f0">${escHtml(s.title)}</span>
                <span class="badge ${s.kind === 'wisdom' ? 'badge--warn' : 'badge--ok'}">${escHtml(s.kind)}</span>
              </div>
              <div style="font-size:11px;color:#94a3b8;white-space:pre-wrap">${escHtml(s.content)}</div>
            </div>`).join('');
        }
      } catch (e) {
        researchBody.innerHTML = `<pre class="json-viewer">${escHtml(researchRun.logs)}</pre>`;
      }
    } else {
      researchBody.innerHTML = `<div style="font-size:12px;color:#94a3b8;padding:8px 0">Research has not run yet</div>`;
    }
  }

  // Work order / agent run output + Spec sub-section
  const outputEl = el('work-order-body');
  if (outputEl && task) {
    const runs = state.agentRuns[task.id] || [];
    const planRun = runs.filter(r => r.agent_name === 'claude' && r.role === 'PLANNING' && r.status === 'completed').slice(-1)[0];
    const specRun = runs.filter(r => r.agent_name === 'claude' && r.role === 'SPEC' && r.status === 'completed').slice(-1)[0];

    let planHtml = `<div style="font-size:12px;color:#94a3b8;padding:8px 0">Work order not yet available</div>`;
    if (planRun?.logs) {
      try {
        const wo = JSON.parse(planRun.logs);
        planHtml = `<div class="json-viewer">${syntaxHighlight(wo)}</div>`;
      } catch(e) {
        planHtml = `<pre class="json-viewer">${escHtml(planRun.logs)}</pre>`;
      }
    }

    let specHtml = '';
    if (specRun?.logs) {
      try {
        const specObj = JSON.parse(specRun.logs);
        specHtml = `
          <div style="margin-top: 12px; border-top: 1px solid var(--clr-border); padding-top: 12px">
            <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#cbd5e1;margin-bottom:6px">📋 Acceptance Criteria Spec</div>
            <div class="json-viewer">${syntaxHighlight(specObj)}</div>
          </div>`;
      } catch(e) {
        specHtml = `
          <div style="margin-top: 12px; border-top: 1px solid var(--clr-border); padding-top: 12px">
            <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#cbd5e1;margin-bottom:6px">📋 Acceptance Criteria Spec</div>
            <pre class="json-viewer">${escHtml(specRun.logs)}</pre>
          </div>`;
      }
    }
    outputEl.innerHTML = planHtml + specHtml;
  }

  // Artifacts
  const artsEl = el('artifacts-body');
  if (artsEl && task) {
    const arts = state.artifacts[task.id] || [];
    if (!arts.length) {
      artsEl.innerHTML = `<div style="font-size:12px;color:#94a3b8;padding:8px 0">No artifacts yet</div>`;
    } else {
      artsEl.innerHTML = arts.map(a => {
        let contentMarkup = '';
        try {
          const parsed = JSON.parse(a.content);
          contentMarkup = `<div class="json-viewer">${syntaxHighlight(parsed)}</div>`;
        } catch (e) {
          contentMarkup = `<pre class="json-viewer">${escHtml(a.content)}</pre>`;
        }
        return `
          <div class="artifact-card fade-in" style="border: 1px solid var(--clr-border); border-radius: var(--r-sm); background: var(--clr-surface); margin-bottom: 8px">
            <div style="display:flex;align-items:center;gap:4px">
              <button class="review-toggle" style="display:flex;flex:1;min-width:0;align-items:center;justify-content:space-between;padding:10px var(--sp-md);background:none;border:none;cursor:pointer;color:inherit;font:inherit">
                <span class="review-toggle-label" style="font-size:12px;font-family:var(--txt-mono);color:#94a3b8;display:flex;align-items:center;gap:8px;min-width:0">
                  <span class="artifact-name" style="color:inherit;overflow:hidden;text-overflow:ellipsis">${escHtml(a.name)}</span>
                  <span class="badge badge--ok">✓</span>
                </span>
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <path d="M4 6l3 3 3-3" stroke="#9ca3af" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </button>
              <button class="btn btn--ghost btn--sm btn-open-artifact" data-id="${a.id}" title="Open in Windows app picker" style="margin-right:8px;white-space:nowrap">Open ↗</button>
            </div>
            <div class="artifact-content" style="display:none;padding:0 var(--sp-md) var(--sp-sm)">
              ${contentMarkup}
            </div>
          </div>`;
      }).join('');

      artsEl.querySelectorAll('.review-toggle').forEach(btn => {
        btn.addEventListener('click', () => {
          const body = btn.closest('.artifact-card')?.querySelector('.artifact-content');
          if (!body) return;
          const open = body.style.display !== 'none';
          body.style.display = open ? 'none' : '';
          const svg = btn.querySelector('svg path');
          if (svg) svg.setAttribute('d', open ? 'M4 8l3-3 3 3' : 'M4 6l3 3 3-3');
        });
      });
      artsEl.querySelectorAll('.btn-open-artifact').forEach(btn => {
        btn.addEventListener('click', async () => {
          try { await API.openArtifact(parseInt(btn.dataset.id, 10)); }
          catch (e) { State.pushLog(`Could not open artifact: ${e.message}`, 'err'); }
        });
      });
    }
  }

  // Audit timeline — grouped into a one-time "Setup" leg (Research/Spec/Plan,
  // each runs once) plus numbered Build↔Verify cycles, instead of one flat
  // chronological list. A retried task can otherwise show 7+ items in a row
  // with no way to tell how many cycles happened or what each one decided.
  const timelineEl = el('timeline-body');
  if (timelineEl && task) {
    const runs = state.agentRuns[task.id] || [];
    if (!runs.length) {
      timelineEl.innerHTML = `<div style="font-size:12px;color:#94a3b8;padding:8px 0">No events yet</div>`;
    } else {
      const dotColors = { codex: '#7c3aed', claude: '#d97706', antigravity: '#0891b2', engine: '#10b981' };
      const tlItem = (r, isLast) => `
        <div class="tl-item">
          <div class="tl-dot-col">
            <div class="tl-dot" style="background:${dotColors[r.agent_name]||'#9ca3af'}"></div>
            ${isLast ? '' : '<div class="tl-line"></div>'}
          </div>
          <div class="tl-text">
            <div class="tl-title">${r.agent_name}${r.role ? ` (${r.role})` : ''} · ${r.status}</div>
            <div class="tl-meta">${r.started_at ? timeAgo(r.started_at) : (r.status === 'pending' ? 'Not started' : '')}</div>
          </div>
        </div>`;
      const groupCard = (borderColor, inner) =>
        `<div style="border:1px solid ${borderColor};border-radius:var(--r-sm);background:var(--clr-surface);margin-bottom:8px;padding:8px 10px">${inner}</div>`;

      // Research/Spec/Plan only ever run once per task - no grouping needed,
      // just list them. Each Build (EXECUTION) run starts a new cycle;
      // Verify (REVIEW) runs attach to whichever cycle is currently open.
      const setup = [];
      const cycles = [];
      let openCycle = null;
      for (const r of runs) {
        if (r.role === 'EXECUTION') {
          openCycle = { execution: r, review: null };
          cycles.push(openCycle);
        } else if (r.role === 'REVIEW') {
          if (!openCycle) { openCycle = { execution: null, review: null }; cycles.push(openCycle); }
          openCycle.review = r;
        } else {
          setup.push(r);
        }
      }

      let html = '';
      if (setup.length) {
        html += groupCard('var(--clr-border)', `
          <div style="font-size:11px;font-weight:600;color:#cbd5e1;margin-bottom:6px">Setup</div>
          <div class="timeline">${setup.map((r, i) => tlItem(r, i === setup.length - 1)).join('')}</div>`);
      }
      cycles.forEach((c, idx) => {
        let verdict = null;
        if (c.review?.status === 'completed') {
          try { verdict = JSON.parse(c.review.logs || '{}').verdict; } catch (e) { /* not JSON, leave null */ }
        }
        let label = 'in progress', color = '#94a3b8', border = 'var(--clr-border)';
        if (c.execution?.status === 'blocked' || c.execution?.status === 'failed' || c.review?.status === 'failed') {
          label = 'blocked'; color = '#fca5a5'; border = '#f87171';
        } else if (verdict === 'approved') {
          label = 'approved'; color = '#6ee7b7'; border = '#6ee7b7';
        } else if (verdict === 'changes requested' || verdict === 'changes_requested') {
          label = 'changes requested'; color = '#fbbf24'; border = '#fde68a';
        }
        const items = [c.execution, c.review].filter(Boolean);
        html += groupCard(border, `
          <div style="display:flex;justify-content:space-between;align-items:baseline;font-size:11px;font-weight:600;margin-bottom:6px">
            <span style="color:#cbd5e1">Cycle ${idx + 1} of ${cycles.length}</span>
            <span style="color:${color}">${label}</span>
          </div>
          <div class="timeline">${items.map((r, i) => tlItem(r, i === items.length - 1)).join('')}</div>`);
      });
      timelineEl.innerHTML = html;
    }
  }
}

// ── Probe section (in F1 bottom) ──────────────────────────────────────────────

function renderProbe(state) {
  const probeEl = el('probe-strip');
  if (!probeEl) return;
  if (!state.probeResults.length) {
    probeEl.innerHTML = `<div class="probe-row">
      <span class="probe-tool" style="color:#94a3b8">Phase 0 probe not run yet</span>
      <button class="btn btn--sm btn--ghost" id="btn-probe">Run Probe</button>
    </div>`;
    el('btn-probe')?.addEventListener('click', API.runProbe);
    return;
  }
  probeEl.innerHTML = state.probeResults.map(r => {
    const cooling = r.quota_status === 'exhausted' && r.cooldown_until && (r.cooldown_until * 1000) > Date.now();
    let quotaBit = '';
    if (cooling) {
      const mins = Math.max(0, Math.round((r.cooldown_until * 1000 - Date.now()) / 60000));
      quotaBit = `<span class="badge badge--warn" title="Skipped by routing until cooldown expires">⏳ ${mins}m</span>
                  <button class="btn btn--ghost btn--sm btn-reset-cap" data-agent="${escHtml(r.tool_name)}" title="Clear this cooldown now">reset</button>`;
    } else if (r.quota_status === 'exhausted') {
      quotaBit = `<span class="badge badge--warn" title="Exhausted, cooldown time unknown">⏳ exhausted</span>`;
    }
    return `
    <div class="probe-row">
      <span class="probe-tool">${escHtml(r.tool_name)}</span>
      <div style="display:flex;align-items:center;gap:6px">
        ${r.available
          ? `<span class="badge badge--ok">✓ ${escHtml(r.version || 'found')}</span>`
          : `<span class="badge badge--err">✗ not found</span>`}
        ${quotaBit}
      </div>
    </div>`;
  }).join('') + `
    <div class="probe-row" style="border-bottom:none">
      <span class="probe-tool" style="color:var(--clr-ok)">memory_store</span>
      <div style="display:flex;align-items:center;gap:6px">
        <span class="badge badge--ok" title="Active indexing tier">FTS5 (SQLite)</span>
      </div>
    </div>`;

  probeEl.querySelectorAll('.btn-reset-cap').forEach(b => {
    b.addEventListener('click', () => API.resetCapability(b.dataset.agent));
  });
}

// ── Project gate (folder browser) ───────────────────────────────────────────

function renderProjectChip(state) {
  const chip = el('project-chip');
  if (!chip) return;
  if (!state.activeProject) { chip.innerHTML = ''; return; }
  chip.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 14px;border-bottom:1px solid var(--clr-border)">
      <div style="min-width:0">
        <div style="font-size:12px;font-weight:600;color:#e2e8f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(state.activeProject.name || '')}</div>
        <div style="font-size:10px;color:#94a3b8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escHtml(state.activeProject.path || '')}">${escHtml(state.activeProject.path || '')}</div>
      </div>
      <button class="btn btn--ghost btn--sm" id="btn-change-project">Change</button>
    </div>`;
  el('btn-change-project')?.addEventListener('click', () => API.openProjectPicker());
}

function renderProjectGate(state) {
  const gate = el('project-gate');
  if (!gate) return;
  const show = state.projectPickerOpen || !state.activeProject;
  gate.style.display = show ? 'flex' : 'none';
  if (!show) return;

  const closeBtn = el('btn-close-picker');
  if (closeBtn) closeBtn.style.display = state.activeProject ? '' : 'none';

  const { path, parent, entries } = state.fsBrowser || {};

  const crumbEl = el('fs-breadcrumb');
  if (crumbEl) {
    crumbEl.innerHTML = `<span class="fs-crumb" data-path="">Drives</span>${path ? ' / ' + escHtml(path) : ''}`;
    crumbEl.querySelector('.fs-crumb')?.addEventListener('click', () => API.browseDir(''));
  }

  const listEl = el('fs-list');
  if (listEl) {
    const rows = [];
    if (parent !== null && parent !== undefined) {
      rows.push(`<div class="fs-row fs-row--up" data-path="${escHtml(parent)}">.. up</div>`);
    }
    if (entries && entries.length) {
      rows.push(...entries.map(e => `<div class="fs-row" data-path="${escHtml(e.path)}">📁 ${escHtml(e.name)}</div>`));
    }
    listEl.innerHTML = rows.length
      ? rows.join('')
      : `<div class="empty" style="padding:20px"><div class="empty-label">No subfolders here.</div></div>`;
    listEl.querySelectorAll('.fs-row').forEach(row => {
      row.addEventListener('click', () => API.browseDir(row.dataset.path));
    });
  }

  const pathEl = el('fs-current-path');
  if (pathEl) pathEl.textContent = path || '(pick a drive or folder above)';

  const selectBtn = el('btn-select-folder');
  if (selectBtn) selectBtn.disabled = !path;
}

// ── Status bar ────────────────────────────────────────────────────────────────

function renderStatusBar(state) {
  const bar = el('status-bar');
  if (!bar) return;
  if (state.serverAvailable) {
    bar.textContent = state.ssConnected ? '⬤ connected' : '○ reconnecting…';
    bar.style.color = state.ssConnected ? '#059669' : '#d97706';
  } else {
    bar.textContent = '✗ backend offline — run: python server.py';
    bar.style.color = '#dc2626';
  }
}

// ── Knowledge panel ──────────────────────────────────────────────────────────

function renderKnowledge(state) {
  const panel = el('knowledge-panel');
  if (!panel) return;
  const count = state.knowledge?.documents?.length || 0;
  panel.innerHTML = `
    <div class="probe-row" style="background: var(--clr-panel); border-bottom: none">
      <span class="probe-tool" style="display:flex;align-items:center;gap:6px">🧠 Memory Store</span>
      <div style="display:flex;align-items:center;gap:6px">
        <span class="badge badge--ok" id="knowledge-count">${count} doc${count === 1 ? '' : 's'}</span>
        <button class="btn btn--sm btn--ghost" id="btn-reindex-knowledge" style="padding: 2px 8px; font-size: 10.5px">Reindex</button>
      </div>
    </div>`;
  el('btn-reindex-knowledge')?.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      await API.reindexKnowledge();
    } catch (e) {
      console.warn('Reindex error:', e);
    } finally {
      btn.disabled = false;
    }
  });
}

// ── Main render ───────────────────────────────────────────────────────────────

function render(state) {
  try { renderProjectGate(state); } catch(e) { console.warn('project gate render:', e); }
  try { renderProjectChip(state); } catch(e) { console.warn('project chip render:', e); }
  try { renderProbe(state); } catch(e) { console.warn('probe render:', e); }
  try { renderKnowledge(state); } catch(e) { console.warn('knowledge render:', e); }
  try { renderCommandCenter(state); } catch(e) { console.warn('f1 render:', e); }
  try { renderDelegateBar(state); } catch(e) { console.warn('delegate bar render:', e); }
  try { renderFocusMode(state); } catch(e) { console.warn('f2 render:', e); }
  try { renderAtomicReview(state); } catch(e) { console.warn('f3 render:', e); }
  try { renderStatusBar(state); } catch(e) { console.warn('status render:', e); }
}

// ── Accordion toggles (Frame 3) ───────────────────────────────────────────────

function wireAccordions() {
  document.querySelectorAll('.review-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const body = btn.nextElementSibling;
      if (!body) return;
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : '';
      const svg = btn.querySelector('svg path');
      if (svg) svg.setAttribute('d', open ? 'M4 8l3-3 3 3' : 'M4 6l3 3 3-3');
    });
  });
}

// ── XSS helper ────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Initialise ────────────────────────────────────────────────────────────────

function wireProjectGate() {
  el('btn-select-folder')?.addEventListener('click', async () => {
    const path = State.get().fsBrowser?.path;
    if (!path) return;
    try {
      await API.selectProject(path);
    } catch (e) {
      State.pushLog(`Could not select folder: ${e.message}`, 'err');
    }
  });
  el('btn-close-picker')?.addEventListener('click', () => API.closeProjectPicker());
}

export function init() {
  State.subscribe(render);
  wireAccordions();
  wireProjectGate();
  render(State.get());
}

export { render };
