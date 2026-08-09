"""Minimal append-only trace (K1: JSONL; K4 upgrades to SQLite/WAL).

Two stores, one truth: files are the raw record. Every event carries the run id and a
monotonic timestamp passed in by the caller (scripts can't call Date.now-equivalents freely,
but this is plain Python, so time.time() is fine here — the workflow-script restriction does
not apply to the control plane)."""
from __future__ import annotations

import json
import time
from pathlib import Path


class Tracer:
    def __init__(self, run_dir: Path, run_id: str):
        self.path = run_dir / "trace.jsonl"
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _emit(self, kind: str, **fields):
        rec = {"ts": round(time.time(), 3), "run": self.run_id, "event": kind, **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def phase_start(self, name, kind, owner):
        self._emit("phase_start", phase=name, kind=kind, owner=owner)

    def phase_end(self, name, status):
        self._emit("phase_end", phase=name, status=status)

    def record_call(self, role, events, cost_usd, timed_out):
        self._emit("agent_call", role=role, events=events, cost_usd=round(cost_usd, 4), timed_out=timed_out)

    def gate(self, report):
        self._emit("gate", gate=report.gate, passed=report.passed, evidence=report.evidence)

    def log(self, **fields):
        self._emit("log", **fields)

    def total_cost(self) -> float:
        if not self.path.exists():
            return 0.0
        tot = 0.0
        for ln in self.path.read_text().splitlines():
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("event") == "agent_call":
                tot += float(r.get("cost_usd", 0))
        return round(tot, 4)
