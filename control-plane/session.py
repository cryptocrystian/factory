"""Run + Phase. A run pins a workspace repo and moves it through phases. Each phase is a
fresh bounded session; state lives on disk. The load-bearing P0 lesson is here: COMMIT at
every phase boundary, so post-hoc write-enforcement (permissions.py, K2) can attribute and
roll back per phase, and so a resumed run does not double-commit (idempotency)."""
from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import config
import envelopes as E
from tracer import Tracer


class GitError(RuntimeError):
    pass


class Run:
    def __init__(self, workspace: Path, lane: str, target: str, run_id: str | None = None):
        self.workspace = Path(workspace).resolve()
        self.lane = lane
        self.target = target
        self.run_id = run_id or f"{time.strftime('%Y-%m-%d-%H%M%S')}-{lane}-{target}"
        self.dir = config.RUNS_DIR / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tracer = Tracer(self.dir, self.run_id)
        self.accepted: bool | None = None
        self._base = self.git("rev-parse", "HEAD").strip()      # pin the base (I4-ish, local)
        # Isolation: run on a branch; only merge to the base when accepted (gated merge, Rev4 P5).
        self.base_branch = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.work_branch = f"factory/{self.run_id}"
        if self.dirty():
            raise GitError("workspace is dirty; a run must start from a clean tree (I4)")
        self.git("checkout", "-q", "-b", self.work_branch)
        self.tracer.log(event_detail="run_start", workspace=str(self.workspace),
                        base=self._base[:8], base_branch=self.base_branch, work_branch=self.work_branch)

    # ---- git in the workspace ------------------------------------------------
    def git(self, *args: str) -> str:
        p = subprocess.run(["git", *args], cwd=str(self.workspace),
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {p.stderr.strip()}")
        return p.stdout

    def dirty(self) -> bool:
        return bool(self.git("status", "--porcelain").strip())

    def commit(self, message: str) -> str:
        """Phase-boundary commit. No-op (not an error) if nothing changed — idempotent."""
        if not self.dirty():
            self.tracer.log(event_detail="commit_skip", reason="clean tree")
            return self.git("rev-parse", "HEAD").strip()
        self.git("add", "-A")
        self.git("-c", "commit.gpgsign=false", "commit", "-m", message)
        sha = self.git("rev-parse", "HEAD").strip()
        self.tracer.log(event_detail="commit", sha=sha[:8], message=message.splitlines()[0])
        return sha

    def snapshot(self) -> dict[str, str]:
        """Fingerprint the working tree (path -> status) for post-hoc write-enforcement (K2)."""
        out = self.git("status", "--porcelain=v1", "-z")
        snap = {}
        for chunk in out.split("\0"):
            if len(chunk) > 3:
                snap[chunk[3:]] = chunk[:2]
        return snap

    # ---- phases --------------------------------------------------------------
    @contextmanager
    def phase(self, params: E.PhaseParams):
        self.tracer.phase_start(params.name, params.kind, params.owner)
        ph = _Phase(self, params)
        status = "fail"                       # status defaults to failure; success is earned (I5)
        try:
            yield ph
            status = ph.status
        finally:
            self.tracer.phase_end(params.name, status)

    def finish(self, accepted: bool, reason: str = "") -> bool:
        self.accepted = accepted
        # Gated merge: accepted -> merge the work branch into the base; else -> leave the base
        # untouched (return the tree to base; the work branch remains for inspection).
        merged = False
        try:
            if not self.dirty():
                self.git("checkout", "-q", self.base_branch)
                if accepted:
                    self.git("-c", "commit.gpgsign=false", "merge", "--no-ff", "-q",
                             "-m", f"merge {self.work_branch} (accepted)", self.work_branch)
                    merged = True
        except GitError as e:
            self.tracer.log(event_detail="merge_error", error=str(e))
        self.tracer.log(event_detail="finish", accepted=accepted, reason=reason, merged=merged,
                        work_branch=self.work_branch, cost_usd=self.tracer.total_cost())
        return accepted


class _Phase:
    def __init__(self, run: Run, params: E.PhaseParams):
        self.run = run
        self.params = params
        self.status = "fail"

    def ok(self):
        self.status = "success"
