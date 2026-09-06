"""Isolation port (Rev4). A run never mutates the shared repo working tree — it ACQUIRES an isolated
workspace, mutates only there, and MERGES back only when accepted, then the workspace is DESTROYED.

Default adapter: git **worktree** — each run gets its own working directory sharing the origin's object
database (local, lightweight, parallel-safe). The origin repo's tree is never touched, so a runaway
child (even after a phase's process tree is reaped) can only dirty a disposable worktree. A container /
remote adapter (exe.dev) can sit behind the same port later — nothing above this line names 'worktree'.

The gated-merge honesty rules live here (residue capture, I11 base-advance refusal), so `Run.finish`
just delegates.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import config


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


@dataclass
class MergeResult:
    merged: bool
    error: str | None = None
    pushed: bool | None = None      # None = not attempted (rejected merge); False = merged, not shipped


class IsolatedWorkspace:
    """A per-run isolated working tree on branch `factory/<run_id>`, forked from the origin's base.
    The run operates in `path`; `origin` (the real repo) is never mutated until an accepted merge."""

    def __init__(self, adapter: "WorktreeIsolation", origin: Path, path: Path,
                 branch: str, base_branch: str, base_commit: str, symlinks: list[Path]):
        self.adapter = adapter
        self.origin = Path(origin)
        self.remote = "origin"        # where an accepted merge is shipped
        self.path = Path(path)
        self.branch = branch
        self.base_branch = base_branch
        self.base_commit = base_commit
        self._symlinks = symlinks

    def git(self, *args: str) -> str:
        return _git(self.path, *args)

    def dirty(self) -> bool:
        return bool(self.git("status", "--porcelain").strip())

    def merge_back(self, accepted: bool) -> MergeResult:
        return self.adapter.merge_back(self, accepted)

    def destroy(self, keep_branch: bool = True) -> None:
        self.adapter.destroy(self, keep_branch)


class WorktreeIsolation:
    """Git-worktree Isolation adapter. `shared_ignored` dirs (e.g. node_modules) are symlinked from the
    origin into the worktree so toolchain gates work without a fresh install (a worktree omits
    gitignored files)."""

    def __init__(self, worktrees_dir: Path, shared_ignored: tuple[str, ...] = ("node_modules",)):
        self.worktrees_dir = Path(worktrees_dir)
        self.shared_ignored = shared_ignored

    def acquire(self, origin: Path, run_id: str, resume_from: str | None = None) -> IsolatedWorkspace:
        """`resume_from` starts the new work branch at an EXISTING branch instead of the base.

        A run that dies on infrastructure or is killed mid-flight leaves a complete, committed build
        on its work branch — 98 such branches exist here, several thousand lines each — and the next
        dispatch threw all of it away and rebuilt from base. JRN-B1 was re-planned and re-built from
        scratch eight times, dying at the reviewer every time, while its finished build sat on a
        branch nothing ever looked at again.

        Only unjudged work may be resumed. A build the reviewer REJECTED is tainted and must be
        rebuilt or remediated against its findings; a build that was never reviewed is simply
        unreviewed. The caller decides which case this is; this method only does what it is told.
        The merge target is unchanged — resuming affects where the branch STARTS, never where it
        lands."""
        origin = Path(origin).resolve()
        base_branch = _git(origin, "rev-parse", "--abbrev-ref", "HEAD").strip()
        base_commit = _git(origin, "rev-parse", "HEAD").strip()
        if bool(_git(origin, "status", "--porcelain").strip()):
            raise GitError("origin repo is dirty; a run must start from a clean tree (I4)")
        branch = f"factory/{run_id}"
        wt = self.worktrees_dir / run_id
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        if wt.exists():
            raise GitError(f"worktree path already exists: {wt}")
        start_at = base_commit
        if resume_from:
            try:
                start_at = _git(origin, "rev-parse", "--verify", f"{resume_from}^{{commit}}").strip()
            except Exception:
                start_at = base_commit          # a missing branch must not stop the run, only un-resume it
        _git(origin, "worktree", "add", "-q", "-b", branch, str(wt), start_at)
        symlinks: list[Path] = []
        for name in self.shared_ignored:
            src, dst = origin / name, wt / name
            if src.exists() and not dst.exists():
                try:
                    os.symlink(src, dst)
                    symlinks.append(dst)
                except OSError:
                    pass
        return IsolatedWorkspace(self, origin, wt, branch, base_branch, base_commit, symlinks)

    def merge_back(self, ws: IsolatedWorkspace, accepted: bool) -> MergeResult:
        # Drop the shared_ignored symlinks (node_modules) BEFORE staging: they are run scaffolding,
        # never content, and a dir-only gitignore (`node_modules/`) does not match a symlink, so an
        # `add -A` would otherwise commit them into origin and leave every future run's tree dirty (I4).
        for link in ws._symlinks:
            try:
                if link.is_symlink():
                    link.unlink()
            except OSError:
                pass
        # Capture residue on the work branch so a missed boundary-commit loses nothing (honesty).
        if ws.dirty():
            ws.git("add", "-A")
            ws.git("-c", "commit.gpgsign=false", "commit", "-m",
                   f"residue({ws.branch}): uncommitted work at finish")
        if not accepted:
            return MergeResult(merged=False)
        base_now = _git(ws.origin, "rev-parse", ws.base_branch).strip()
        if base_now != ws.base_commit:                       # I11: base moved under us
            return MergeResult(merged=False,
                               error=f"base advanced {ws.base_commit[:8]}->{base_now[:8]} (I11); refusing merge")
        try:
            # merge FROM the work branch INTO the origin's base branch (origin stays on base_branch;
            # the branch is checked out in the worktree, which git permits for a merge source).
            _git(ws.origin, "-c", "commit.gpgsign=false", "merge", "--no-ff", "-q",
                 "-m", f"merge {ws.branch} (accepted)", ws.branch)
        except GitError as e:
            return MergeResult(merged=False, error=str(e))
        # PUSH. Without this the merge lands on the box's local branch and stops there: JRN-S4's
        # entire accepted build — ten commits, a migration, 3,800 lines — sat only on the VPS until
        # 2026-08-21, while the handoff claimed GitHub was the origin of truth. A merge nobody else
        # can see is not shipped, and a lost box loses the work.
        #
        # The merge itself has already committed, so a push failure is REPORTED, not raised: the
        # work is safe locally and a later push (or the next accepted merge) carries it. What must
        # never happen is a silent success.
        try:
            _git(ws.origin, "push", ws.remote, ws.base_branch)
            return MergeResult(merged=True, pushed=True)
        except GitError as e:
            return MergeResult(merged=True, pushed=False, error=f"merged locally but push failed: {e}")

    def destroy(self, ws: IsolatedWorkspace, keep_branch: bool = True) -> None:
        for link in ws._symlinks:                            # drop symlinks first (never the targets)
            try:
                if link.is_symlink():
                    link.unlink()
            except OSError:
                pass
        try:
            _git(ws.origin, "worktree", "remove", "--force", str(ws.path))
        except GitError:
            shutil.rmtree(ws.path, ignore_errors=True)
            try:
                _git(ws.origin, "worktree", "prune")
            except GitError:
                pass
        if not keep_branch:
            try:
                _git(ws.origin, "branch", "-D", ws.branch)
            except GitError:
                pass


def default_isolation() -> WorktreeIsolation:
    """The configured Isolation port. Worktrees live under the factory's runs dir (gitignored)."""
    return WorktreeIsolation(Path(config.RUNS_DIR) / "_worktrees")
