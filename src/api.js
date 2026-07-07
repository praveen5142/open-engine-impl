/**
 * src/api.js  –  REST client + SSE connection to Python backend
 */
import State from './state.js';

const BASE = '';

// ── Generic fetch wrapper ──────────────────────────────────────────────────────

async function api(method, path, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(`${BASE}${path}`, opts);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: resp.statusText }));
    throw new Error(err.error || resp.statusText);
  }
  return resp.status === 204 ? null : resp.json();
}

export const api_get  = (path) => api('GET',  path);
export const api_post = (path, body) => api('POST', path, body);

// ── Data loaders ──────────────────────────────────────────────────────────────

export async function loadTasks() {
  const proj = State.get().activeProject;
  const qs = proj && proj.path ? `?project=${encodeURIComponent(proj.path)}` : '';
  const tasks = await api_get(`/api/tasks${qs}`);
  State.set({ tasks });
  State.pushLog(`Loaded ${tasks.length} tasks`, 'ok');
}

export async function loadTask(taskId) {
  const task = await api_get(`/api/tasks/${taskId}`);
  State.set(s => ({
    tasks: s.tasks.map(t => t.id === taskId ? { ...t, ...task } : t),
    agentRuns: { ...s.agentRuns, [taskId]: task.agent_runs || [] },
  }));
  // Also load artifacts
  await loadArtifacts(taskId);
}

export async function loadArtifacts(taskId) {
  const arts = await api_get(`/api/artifacts/${taskId}`);
  State.set(s => ({ artifacts: { ...s.artifacts, [taskId]: arts } }));
}

export async function openArtifact(artifactId) {
  const r = await api_post(`/api/artifacts/${artifactId}/open`);
  State.pushLog(`Opened artifact in app picker: ${r.path}`, 'ok');
  return r;
}

export async function loadApprovals() {
  const gates = await api_get('/api/approval');
  State.set({ pendingApprovals: gates });
}

export async function loadProbe() {
  const probe = await api_get('/api/probe');
  State.set({ probeResults: probe });
}

export async function loadKnowledge() {
  try {
    const knowledge = await api_get('/api/knowledge');
    State.set({ knowledge });
  } catch (e) {
    State.pushLog(`Error loading knowledge: ${e.message}`, 'err');
  }
}

export async function reindexKnowledge() {
  State.pushLog('Reindexing knowledge base...', 'info');
  try {
    const result = await api_post('/api/knowledge/reindex', {});
    State.pushLog(`Reindex complete: ${result.created} created, ${result.updated} updated, ${result.unchanged} unchanged`, 'ok');
    await loadKnowledge();
    return result;
  } catch (e) {
    State.pushLog(`Reindex failed: ${e.message}`, 'err');
    throw e;
  }
}

// ── Actions ────────────────────────────────────────────────────────────────────

export async function createTask(title, description = '', verifyCommand = '') {
  const task = await api_post('/api/tasks', { title, description, verify_command: verifyCommand });
  State.set(s => ({ tasks: s.tasks.some(t => t.id === task.id) ? s.tasks : [task, ...s.tasks], activeTaskId: task.id }));
  State.pushLog(`Task #${task.id} created`, 'ok');
  return task;
}

export async function runProbe() {
  State.pushLog('Running Phase 0 capability probe…', 'info');
  const results = await api_post('/api/probe');
  State.set({ probeResults: Object.entries(results).map(([name, info]) => ({ tool_name: name, ...info })) });
  State.pushLog('Probe complete', 'ok');
  return results;
}

export async function pollAntigravity(taskId) {
  const result = await api_get(`/api/run/${taskId}/poll`);
  await loadTask(taskId);
  if (result.artifacts_found > 0) {
    await loadArtifacts(taskId);
    State.pushLog(`Antigravity returned ${result.artifacts_found} artifact(s)!`, 'ok');
  }
  return result;
}

export async function approveGate(gateId) {
  const r = await api_post('/api/approval', { id: gateId, decision: 'approved' });
  await loadApprovals();
  State.pushLog(`Gate #${gateId} approved`, 'ok');
  return r;
}

export async function rejectGate(gateId) {
  const r = await api_post('/api/approval', { id: gateId, decision: 'rejected' });
  await loadApprovals();
  State.pushLog(`Gate #${gateId} rejected`, 'warn');
  return r;
}

// ── Project selection & folder browser ─────────────────────────────────────────
// No native OS file dialog is available (this is a plain stdlib HTTP server,
// not an Electron app), so project selection is an in-app folder browser
// backed by GET /api/fs/list.

export async function loadProject() {
  const project = await api_get('/api/project');
  State.set({ activeProject: project });
  return project;
}

export async function selectProject(path, name) {
  const project = await api_post('/api/project', { path, name });
  State.set({ activeProject: project, projectPickerOpen: false });
  State.pushLog(`Project selected: ${project.name} (${project.path})`, 'ok');
  await loadTasks();
  return project;
}

export async function browseDir(path = '') {
  const qs = path ? `?path=${encodeURIComponent(path)}` : '';
  const result = await api_get(`/api/fs/list${qs}`);
  State.set({ fsBrowser: result });
  return result;
}

export function openProjectPicker() {
  State.set({ projectPickerOpen: true });
  const current = State.get().activeProject;
  browseDir(current ? current.path : '').catch(err => State.pushLog(`Browse error: ${err.message}`, 'err'));
}

export function closeProjectPicker() {
  State.set({ projectPickerOpen: false });
}

// ── Delegate Routing (autonomous, single-button) ────────────────────────────

export async function runTask(taskId) {
  State.pushLog(`Running task #${taskId} (Delegate Routing)…`, 'info');
  const result = await api_post(`/api/tasks/${taskId}/delegate`, {});
  await loadTask(taskId);
  for (const step of result.steps || []) {
    if (step.status === 'blocked') {
      State.pushLog(`${step.role} → HOLD: ${step.reason}`, 'err');
    } else if (step.status === 'completed') {
      const verdict = step.role === 'REVIEW' ? ` (${step.result?.verdict})` : '';
      State.pushLog(`${step.role} → ${step.agent} · completed${verdict}`, 'ok');
    } else {
      State.pushLog(`${step.role} failed: ${step.reason || 'unknown error'}`, 'err');
    }
  }
  if (!result.steps || !result.steps.length) {
    State.pushLog('Nothing to run — task already complete or needs a human.', 'warn');
  }
  return result;
}

export async function resetCapability(agent) {
  const r = await api_post('/api/capability/reset', { agent });
  await loadProbe();
  State.pushLog(`${agent}: cooldown cleared manually`, 'warn');
  return r;
}

export async function submitArtifact(taskId, name, content) {
  const r = await api_post('/api/artifacts', { task_id: taskId, name, content });
  await loadArtifacts(taskId);
  State.pushLog(`Artifact "${name}" submitted`, 'ok');
  return r;
}

// ── Live-log detail formatting ──────────────────────────────────────────────
// Pulls the just-completed PLANNING/REVIEW/EXECUTION run's own content into
// the live log, instead of just a one-line "done" notice. Reuses agentRuns
// already refreshed by loadTask() (called by the handlers below) rather than
// adding a second endpoint - GET /api/tasks/<id> already returns agent_runs.

function _latestRun(taskId, role) {
  const runs = State.get().agentRuns[taskId] || [];
  return runs.filter(r => r.role === role && r.status === 'completed').slice(-1)[0];
}

function _truncate(text, max) {
  return text.length > max ? text.slice(0, max) + '…' : text;
}

function _logWorkOrder(wo) {
  if (wo.task_summary) State.pushLog(`Plan: ${wo.task_summary}`, 'ok');
  for (const step of wo.implementation_steps || []) {
    State.pushLog(`  • ${_truncate(String(step), 80)}`, 'info');
  }
  const risk = wo.risk_assessment;
  if (risk?.level) {
    State.pushLog(`Risk: ${risk.level}${risk.notes ? ' — ' + risk.notes : ''}`, risk.level === 'low' ? 'ok' : 'warn');
  }
  if (wo.recommended_action) State.pushLog(`Recommended action: ${wo.recommended_action}`, 'info');
}

function _logReview(review) {
  State.pushLog(`Review verdict: ${review.verdict || 'unknown'}`, review.verdict === 'approved' ? 'ok' : 'warn');
  if (review.feedback) State.pushLog(_truncate(review.feedback, 200), 'info');
}

function _logExecutionOutput(logs) {
  const lines = (logs.stdout || '').split('\n').map(l => l.trim()).filter(Boolean).slice(0, 20);
  lines.forEach(line => State.pushLog(line, 'info'));
}

// ── SSE connection ─────────────────────────────────────────────────────────────

let _sseSource = null;

const SSE_HANDLERS = {
  task_created:          (d) => { State.set(s => ({ tasks: s.tasks.some(t => t.id === d.id) ? s.tasks : [d, ...s.tasks] })); },
  telemetry:             (d) => { State.set(s => ({ telemetry: [...s.telemetry.slice(-49), d] })); },
  approval_updated:      (d) => { State.set(s => ({ pendingApprovals: s.pendingApprovals.filter(g => g.id !== d.id) })); },
  task_updated:          (d) => { State.set(s => ({ tasks: s.tasks.map(t => t.id === d.id ? { ...t, ...d } : t) })); if (State.get().activeTaskId === d.id) loadTask(d.id); },
  artifact_created:      (d) => { State.pushLog(`Artifact received: ${d.name}`, 'ok'); },
  probe_completed:       (d) => { State.set({ probeResults: Object.entries(d).map(([n,i]) => ({tool_name:n,...i})) }); },
  claude_completed:      async (d) => {
    State.pushLog(`Claude (${d.role || '?'}) done for task #${d.task_id}`, 'ok');
    if (State.get().activeTaskId !== d.task_id) return;
    await loadTask(d.task_id);
    const run = _latestRun(d.task_id, d.role);
    if (!run?.logs) return;
    try {
      const logs = JSON.parse(run.logs);
      if (d.role === 'PLANNING') _logWorkOrder(logs);
      else if (d.role === 'REVIEW') _logReview(logs);
    } catch (e) { /* logs weren't JSON-parseable, skip detail formatting */ }
  },
  claude_failed:         (d) => { State.pushLog(`Claude failed: ${d.error}`, 'err'); if(State.get().activeTaskId===d.task_id) loadTask(d.task_id); },
  antigravity_status:    async (d) => {
    State.pushLog(`Antigravity: ${d.status}`, d.status==='blocked'?'err':'info');
    if (State.get().activeTaskId !== d.task_id) return;
    await loadTask(d.task_id);
    if (d.status !== 'completed') return;
    const run = _latestRun(d.task_id, 'EXECUTION');
    if (!run?.logs) return;
    try { _logExecutionOutput(JSON.parse(run.logs)); } catch (e) { /* not JSON, skip */ }
  },
  antigravity_completed: (d) => { State.pushLog(`Antigravity complete (${(d.artifacts || []).length} artifacts)`, 'ok'); if(State.get().activeTaskId===d.task_id) loadTask(d.task_id); },
  hold_declared:         (d) => { State.pushLog(`HOLD on task #${d.task_id} (${d.role}): ${d.reason}`, 'err'); if(State.get().activeTaskId===d.task_id) loadTask(d.task_id); },
  project_selected:      (d) => { State.pushLog(`Project changed: ${d.name}`, 'info'); },
  capability_reset:      (d) => { State.pushLog(`${d.agent}: cooldown cleared`, 'warn'); loadProbe(); },
  knowledge_reindexed:   (d) => { State.pushLog(`Knowledge base reindexed: ${d.created} created, ${d.updated} updated, ${d.unchanged} unchanged`, 'ok'); loadKnowledge(); },
};

export function connectSSE() {
  if (_sseSource) return;
  State.pushLog('Connecting to backend…', 'info');

  _sseSource = new EventSource(`${BASE}/api/events`);

  _sseSource.onopen = () => {
    State.set({ ssConnected: true, serverAvailable: true });
    State.pushLog('Backend connected ✓', 'ok');
  };

  _sseSource.onerror = () => {
    State.set({ ssConnected: false });
    State.pushLog('Backend disconnected – retrying…', 'warn');
    _sseSource = null;
    setTimeout(connectSSE, 4000);
  };

  for (const [eventName, handler] of Object.entries(SSE_HANDLERS)) {
    _sseSource.addEventListener(eventName, (e) => {
      try { handler(JSON.parse(e.data)); } catch(err) { console.warn('SSE parse error', err); }
    });
  }
}

// ── Health check before SSE ────────────────────────────────────────────────────

export async function checkServer() {
  try {
    await fetch(`${BASE}/api/tasks`, { signal: AbortSignal.timeout(2000) });
    State.set({ serverAvailable: true });
    return true;
  } catch {
    State.set({ serverAvailable: false });
    return false;
  }
}
