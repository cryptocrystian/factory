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
import isolation
import envelopes as E
from tracer import Tracer


class GitError(RuntimeError):
    pass


class Run:
    def __init__(self, workspace: Path, lane: str, target: str, run_id: str | None = None,
                 isolation_port=None):
        self.origin = Path(workspace).resolve()                 # the real repo — never mutated by a run
        self.lane = lane
        self.target = target
        self.run_id = run_id or f"{time.strftime('%Y-%m-%d-%H%M%S')}-{lane}-{target}"
        self.dir = config.RUNS_DIR / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tracer = Tracer(self.dir, self.run_id, lane=lane, target=target)
        self.accepted: bool | None = None
        # Isolation port: acquire an isolated worktree; the run mutates only there and merges back to the
        # base only when accepted (gated merge, Rev4 P5). The origin repo's working tree is never touched.
        self._iso_port = isolation_port or isolation.default_isolation()
        self._iso = self._iso_port.acquire(self.origin, self.run_id)
        self.workspace = self._iso.path                         # where the run (and its agents) operate
        self._base = self._iso.base_commit
        self.base_branch = self._iso.base_branch
        self.work_branch = self._iso.branch
        self.tracer.log(event_detail="run_start", origin=str(self.origin), workspace=str(self.workspace),
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
        """Gated merge via the Isolation port. Honesty invariant: the returned value is the TRUE
        outcome — an accepted run only returns True if the work actually merged onto the base. An
        accepted run that can't merge (residual work, a conflict, or the base advanced under us: I11)
        is downgraded to a non-success with a reason, so the orchestrator escalates instead of
        believing the base advanced. The worktree is destroyed; its branch is kept for inspection on
        any non-success."""
        self.accepted = accepted
        res = self._iso.merge_back(accepted)
        if res.error:
            self.tracer.log(event_detail="merge_error", error=res.error)
        result = accepted and res.merged
        if accepted and not res.merged:
            reason = ((reason + " | ") if reason else "") + (res.error or "accepted but merge did not complete")
        # A merge that never reached the remote is not shipped. Keep the branch and say so loudly:
        # silence here is what stranded JRN-S4's entire build on one box for a day.
        if res.merged and res.pushed is False:
            self.tracer.log(event_detail="merge_not_pushed", error=res.error, branch=self.work_branch)
        self.tracer.log(event_detail="finish", accepted=accepted, merged=res.merged,
                        pushed=res.pushed, result=result,
                        reason=reason, work_branch=self.work_branch, cost_usd=self.tracer.total_cost())
        self._iso.destroy(keep_branch=not (result and res.pushed is not False))
        return result


class _Phase:
    def __init__(self, run: Run, params: E.PhaseParams):
        self.run = run
        self.params = params
        self.status = "fail"

    def ok(self):
        self.status = "success"
