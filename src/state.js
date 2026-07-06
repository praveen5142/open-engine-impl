/**
 * src/state.js  –  Reactive State Store
 * Simple pub/sub state that drives all UI components without a framework.
 */

export const State = (() => {
  const _state = {
    tasks: [],
    activeTaskId: null,
    agentRuns: {},        // taskId → [run, ...]
    pendingApprovals: [],
    artifacts: {},        // taskId → [artifact, ...]
    probeResults: [],
    telemetry: [],        // last N events
    liveLog: [],          // [{text, cls}] – for the live log terminal
    ssConnected: false,
    serverAvailable: false,
    activeProject: null,        // {path, name} or null → gates the dashboard
    projectPickerOpen: false,   // true while the folder-browser overlay is shown
    fsBrowser: { path: '', parent: null, entries: [] },
  };

  const _listeners = new Set();

  function get() { return _state; }

  function set(patch) {
    Object.assign(_state, typeof patch === 'function' ? patch(_state) : patch);
    _listeners.forEach(fn => fn(_state));
  }

  function subscribe(fn) {
    _listeners.add(fn);
    return () => _listeners.delete(fn);
  }

  // Convenience helpers
  function activeTask() {
    return _state.tasks.find(t => t.id === _state.activeTaskId) || null;
  }

  function runsForTask(taskId) {
    return _state.agentRuns[taskId] || [];
  }

  function artifactsForTask(taskId) {
    return _state.artifacts[taskId] || [];
  }

  function pushLog(text, cls = 'info') {
    const entry = { text, cls, ts: new Date().toISOString() };
    set(s => ({ liveLog: [...s.liveLog.slice(-99), entry] }));
  }

  return { get, set, subscribe, activeTask, runsForTask, artifactsForTask, pushLog };
})();

export default State;
