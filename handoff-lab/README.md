# handoff-lab/README.md
# Handoff Lab — Proof Fixture Directory

This directory is the **only** path where the backend will accept file writes
and artifact submissions. All agents must write their proof output here.

## Structure

- `antigravity_inbox.json` — written by the backend when Antigravity leg runs
- `antigravity_return_*.json` — drop files here to complete the Antigravity leg
- `*.md / *.json` — other proof artifacts ingested into SQLite

## Safety Rules

- The backend enforces `ALLOWED_WRITE_PREFIX = handoff-lab/`
- Files outside this directory will be rejected with HTTP 403
- Secret-looking payloads are rejected by the secret scrubber
