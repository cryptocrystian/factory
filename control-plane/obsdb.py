"""The queryable mirror of the trace — SQLite (WAL), one schema, two readers.

Two stores, one truth: the per-run `trace.jsonl` is the raw record; this DB is the queryable
mirror the observability UI reads. `tracer.py` writes it live; `observe.py` reads it and can
backfill history from the JSONL. Deleting the DB loses nothing — `ingest_run` rebuilds it from
files. WAL + busy_timeout make it safe for concurrent runs (and, later, for reads while a remote
run streams in)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import config

DB_PATH = config.RUNS_DIR / "factory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  lane TEXT, target TEXT, workspace TEXT,
  base_branch TEXT, work_branch TEXT,
  started_at REAL, finished_at REAL,
  accepted INTEGER, merged INTEGER, reason TEXT, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, ts REAL,
  event TEXT, phase TEXT, kind TEXT, owner TEXT,
  role TEXT, gate TEXT, passed INTEGER, status TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS ix_runs_started ON runs(started_at);
"""

_COLS = ("phase", "kind", "owner", "role", "gate", "passed", "status")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def upsert_run(conn, run_id, *, lane=None, target=None, workspace=None,
               base_branch=None, work_branch=None, started_at=None):
    conn.execute(
        "INSERT INTO runs(run_id,lane,target,workspace,base_branch,work_branch,started_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
        "lane=COALESCE(excluded.lane,runs.lane), target=COALESCE(excluded.target,runs.target), "
        "workspace=COALESCE(excluded.workspace,runs.workspace), "
        "base_branch=COALESCE(excluded.base_branch,runs.base_branch), "
        "work_branch=COALESCE(excluded.work_branch,runs.work_branch), "
        "started_at=COALESCE(runs.started_at,excluded.started_at)",
        (run_id, lane, target, workspace, base_branch, work_branch, started_at))
    conn.commit()


def record_event(conn, run_id: str, rec: dict):
    """rec is one trace event: {ts, event, ...fields}. Insert it and maintain the run row."""
    ts = rec.get("ts")
    evt = rec.get("event")
    passed = rec.get("passed")
    cols = {c: rec.get(c) for c in _COLS}
    detail = {k: v for k, v in rec.items() if k not in ("ts", "run", "event", *_COLS)}
    conn.execute(
        "INSERT INTO events(run_id,ts,event,phase,kind,owner,role,gate,passed,status,detail) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, ts, evt, cols["phase"], cols["kind"], cols["owner"], cols["role"],
         cols["gate"], (1 if passed else 0) if passed is not None else None,
         cols["status"], json.dumps(detail) if detail else None))
    # run-row maintenance from the narrator log events
    d = rec.get("event_detail")
    if d == "run_start":
        conn.execute("UPDATE runs SET started_at=?, workspace=COALESCE(?,workspace), "
                     "base_branch=COALESCE(?,base_branch), work_branch=COALESCE(?,work_branch) WHERE run_id=?",
                     (ts, rec.get("workspace"), rec.get("base_branch"), rec.get("work_branch"), run_id))
    elif d == "finish":
        acc = rec.get("accepted")
        mg = rec.get("merged")
        conn.execute("UPDATE runs SET finished_at=?, accepted=?, merged=?, reason=?, cost_usd=? WHERE run_id=?",
                     (ts, 1 if acc else 0 if acc is not None else None,
                      1 if mg else 0 if mg is not None else None,
                      rec.get("reason"), rec.get("cost_usd"), run_id))
    conn.commit()


# --------------------------------------------------------------------------- backfill
def _parse_run_id(run_id: str):
    """Best-effort lane/target from a `YYYY-MM-DD-HHMMSS-<lane>-<target>` id."""
    parts = run_id.split("-")
    if len(parts) >= 6 and parts[0].isdigit() and len(parts[0]) == 4:
        return parts[4], "-".join(parts[5:])
    return None, run_id


def ingest_run(conn, run_dir: Path):
    """Rebuild one run's rows from its trace.jsonl (idempotent — clears then reloads)."""
    run_id = run_dir.name
    trace = run_dir / "trace.jsonl"
    if not trace.exists():
        return False
    conn.execute("DELETE FROM events WHERE run_id=?", (run_id,))
    lane, target = _parse_run_id(run_id)
    upsert_run(conn, run_id, lane=lane, target=target)
    for ln in trace.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        record_event(conn, run_id, rec)
    # fallbacks: started/finished/cost derived from events if the log events were absent
    row = conn.execute("SELECT started_at, finished_at, cost_usd FROM runs WHERE run_id=?", (run_id,)).fetchone()
    mn, mx = conn.execute("SELECT MIN(ts), MAX(ts) FROM events WHERE run_id=?", (run_id,)).fetchone()
    cost = conn.execute("SELECT COALESCE(SUM(json_extract(detail,'$.cost_usd')),0) FROM events "
                        "WHERE run_id=? AND event='agent_call'", (run_id,)).fetchone()[0]
    conn.execute("UPDATE runs SET started_at=COALESCE(started_at,?), finished_at=COALESCE(finished_at,?), "
                 "cost_usd=COALESCE(cost_usd,?) WHERE run_id=?", (mn, mx, cost, run_id))
    conn.commit()
    return True


def ingest_all(conn, runs_dir: Path = config.RUNS_DIR):
    n = 0
    for d in sorted(runs_dir.iterdir()):
        if d.is_dir() and (d / "trace.jsonl").exists():
            if ingest_run(conn, d):
                n += 1
    return n


# --------------------------------------------------------------------------- reads
def list_runs(conn, limit=200):
    rows = conn.execute(
        "SELECT r.*, "
        "(SELECT COUNT(*) FROM events e WHERE e.run_id=r.run_id AND e.event='phase_start') AS phases, "
        "(SELECT COUNT(*) FROM events e WHERE e.run_id=r.run_id AND e.event='gate' AND e.passed=0) AS gates_failed "
        "FROM runs r ORDER BY COALESCE(r.started_at,0) DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_run(conn, run_id):
    r = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not r:
        return None
    evs = conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    out = dict(r)
    out["events"] = [dict(e) for e in evs]
    return out
