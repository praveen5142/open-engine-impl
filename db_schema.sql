-- Open Engine Handoff MVP - SQLite Schema
-- Run: python -c "import sqlite3,pathlib; db=sqlite3.connect('db.sqlite'); db.executescript(pathlib.Path('db_schema.sql').read_text()); db.close(); print('OK')"

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  description TEXT,
  project_path TEXT,
  verify_command TEXT,
  status      TEXT CHECK(status IN ('pending','active','completed','blocked')) DEFAULT 'pending',
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS active_project (
  id          INTEGER PRIMARY KEY CHECK(id = 1),
  path        TEXT NOT NULL,
  name        TEXT,
  selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  agent_name   TEXT CHECK(agent_name IN ('claude','antigravity','engine')),
  role         TEXT CHECK(role IN ('RESEARCH','SPEC','PLANNING','EXECUTION','REVIEW')),
  status       TEXT CHECK(status IN ('pending','running','completed','failed','blocked','quota_exceeded','skipped_degraded')) DEFAULT 'pending',
  started_at   TIMESTAMP,
  completed_at TIMESTAMP,
  logs         TEXT
);

CREATE TABLE IF NOT EXISTS telemetry_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload    TEXT,                          -- JSON string
  ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS approval_gates (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  agent_run_id  INTEGER REFERENCES agent_runs(id),
  action_type   TEXT CHECK(action_type IN ('write_file','execute_command','review_decision')),
  payload       TEXT NOT NULL,              -- JSON describing proposed action
  status        TEXT CHECK(status IN ('pending','approved','rejected')) DEFAULT 'pending',
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  reviewed_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  path       TEXT NOT NULL,
  content    TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS capability_probe (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tool_name   TEXT NOT NULL,
  path        TEXT,
  version     TEXT,
  available   INTEGER CHECK(available IN (0,1)) DEFAULT 0,
  error_msg   TEXT,
  probed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  quota_status TEXT CHECK(quota_status IN ('available','exhausted','unknown')) DEFAULT 'unknown',
  quota_confidence REAL DEFAULT 1.0,
  quota_evidence TEXT,
  cooldown_until TIMESTAMP
);

CREATE TABLE IF NOT EXISTS routing_decisions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  role          TEXT NOT NULL,
  requested_agent TEXT,
  chosen_agent  TEXT,
  reason        TEXT,          -- 'primary_available' | 'fallback_quota' | 'fallback_unavailable' | 'hold'
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Phase 1: Open-Brain memory layer (FTS5 keyword search + optional vector tier)
CREATE TABLE IF NOT EXISTS knowledge_documents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_path  TEXT,
  title        TEXT NOT NULL,
  content      TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  kind         TEXT CHECK(kind IN ('rule','wisdom','reference')) NOT NULL,
  task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- FTS5 external-content table: triggers below keep it in sync with knowledge_documents.
-- Without the triggers the FTS index silently goes stale.
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  title, content, content='knowledge_documents', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS knowledge_documents_ai AFTER INSERT ON knowledge_documents BEGIN
  INSERT INTO knowledge_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_documents_ad AFTER DELETE ON knowledge_documents BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_documents_au AFTER UPDATE ON knowledge_documents BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content);
  INSERT INTO knowledge_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;

-- Optional vector tier: populated only if an embedding provider is configured.
-- Created regardless so a fresh install doesn't need conditional logic.
CREATE TABLE IF NOT EXISTS knowledge_vectors (
  doc_id      INTEGER REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  chunk_text  TEXT NOT NULL,
  embedding   BLOB,
  PRIMARY KEY (doc_id, chunk_index)
);
