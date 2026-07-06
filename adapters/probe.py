"""
adapters/probe.py  –  Phase 0 Capability Probe
Detects claude, codex, agy/antigravity, records results in SQLite.
"""
import subprocess
import sqlite3
import shutil
import os
import sys

# Windows cp1252 safe output
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf-16'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

TOOLS = {
    "claude": {
        "known_path": None,
        "version_cmd": ["--version"],
    },
    "codex": {
        "known_path": None,
        "version_cmd": ["--version"],
    },
    "antigravity": {
        "known_path": None,
        "known_path_candidates": [],
        "version_cmd": ["--version"],
    },
}

SEARCH_NAMES = {
    "antigravity": ["agy", "agy.exe", "agy.cmd", "antigravity", "antigravity.exe", "antigravity.cmd"],
}


def run_quiet(cmd, timeout=10):
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError as e:
        return -1, "", str(e)
    except subprocess.TimeoutExpired:
        return -2, "", "Timed out"
    except Exception as e:
        return -3, "", str(e)


def find_tool(tool_name, tool_cfg):
    """Return (path, version, available, error)."""
    kp = tool_cfg.get("known_path")
    if kp and os.path.isfile(kp):
        rc, out, err = run_quiet([kp] + tool_cfg["version_cmd"])
        if rc == 0 and out:
            return kp, out, True, None
        return kp, None, True, err or None

    for candidate_path in tool_cfg.get("known_path_candidates", []):
        if os.path.isfile(candidate_path):
            rc, out, err = run_quiet([candidate_path] + tool_cfg["version_cmd"])
            if rc == 0 and out:
                return candidate_path, out, True, None
            return candidate_path, None, True, err or None

    for candidate in SEARCH_NAMES.get(tool_name, [tool_name]):
        path = shutil.which(candidate)
        if path:
            rc, out, err = run_quiet([path] + tool_cfg["version_cmd"])
            return path, out if rc == 0 else None, True, err or None

    return None, None, False, f"{tool_name} not found on PATH or known location"


def _prior_quota_state(cur, tool_name):
    row = cur.execute(
        """SELECT quota_status, quota_confidence, cooldown_until
           FROM capability_probe WHERE tool_name = ? ORDER BY id DESC LIMIT 1""",
        (tool_name,),
    ).fetchone()
    if row:
        return row[0] or "unknown", row[1] if row[1] is not None else 1.0, row[2]
    return "unknown", 1.0, None


def run_probe(db_path="db.sqlite"):
    """Probe all tools and store results in the DB."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    results = {}
    for tool_name, tool_cfg in TOOLS.items():
        path, version, available, error = find_tool(tool_name, tool_cfg)
        quota_status, quota_confidence, cooldown_until = _prior_quota_state(cur, tool_name)
        cur.execute(
            """INSERT INTO capability_probe
               (tool_name, path, version, available, error_msg,
                quota_status, quota_confidence, cooldown_until)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tool_name, path, version, 1 if available else 0, error,
             quota_status, quota_confidence, cooldown_until),
        )
        results[tool_name] = {
            "path": path,
            "version": version,
            "available": available,
            "error": error,
            "quota_status": quota_status,
            "cooldown_until": cooldown_until,
        }
        status_str = f"[OK] {path} ({version})" if available else f"[X] {error}"
        print(f"  [{tool_name}] {status_str}")

    conn.commit()
    conn.close()
    return results


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "db.sqlite"
    print(f"\n=== Phase 0: Capability Probe (db={db}) ===")
    r = run_probe(db)
    print("\nSummary:")
    for name, info in r.items():
        tag = "PASS" if info["available"] else "BLOCKED"
        print(f"  {tag:8s} {name}")
