"""Post-hoc write-enforcement (I6). `tools:` is capability, `writes:` is the boundary.

We do not intercept writes (bash can run anything, write reaches any path). We compare the
tree after a phase against the grant and ROLL BACK anything outside it — which also catches
reversions (a path modified before and clean after was reverted; reversion is modification).
A breach ABORTS the phase; it is not a gate the agent can redo, because the write already
happened. This is exactly what would have caught the P0 test-author editing 9 source files."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import config


@dataclass
class PermissionResult:
    ok: bool
    breaches: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)


def _glob_to_re(glob: str) -> re.Pattern:
    # ** crosses directories; * stops at '/'. Anchored full-path match.
    out, i = [], 0
    while i < len(glob):
        if glob[i:i + 2] == "**":
            out.append(".*"); i += 2
            if i < len(glob) and glob[i] == "/":
                i += 1
        elif glob[i] == "*":
            out.append("[^/]*"); i += 1
        else:
            out.append(re.escape(glob[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches_any(path: str, globs: list[str]) -> bool:
    return any(_glob_to_re(g).match(path) for g in globs)


def permitted(path: str, role: str) -> bool:
    grant = config.WRITE_GRANTS.get(role)
    granted = grant is None or (grant and _matches_any(path, grant))   # None=unrestricted, []=read-only
    if not granted:
        return False
    # Protected paths are forbidden even to an unrestricted grant, UNLESS the grant names them.
    if _matches_any(path, list(config.PROTECTED_PATHS)):
        return bool(grant) and _matches_any(path, grant) and grant != ["**"]
    return True


def _changed_paths(run) -> list[tuple[str, str]]:
    out = run.git("status", "--porcelain=v1", "-z")
    changes = []
    for chunk in out.split("\0"):
        if len(chunk) > 3:
            changes.append((chunk[:2], chunk[3:]))
    return changes


def enforce(run, role: str) -> PermissionResult:
    """After a phase: roll back every change outside `role`'s grant; a breach fails the phase."""
    res = PermissionResult(ok=True)
    for status, path in _changed_paths(run):
        if permitted(path, role):
            continue
        res.ok = False
        res.breaches.append(f"{status.strip()} {path}")
        # roll back: revert tracked modifications/deletions; remove untracked additions
        if "?" in status:
            p = run.workspace / path
            if p.exists():
                p.unlink()
        else:
            try:
                run.git("checkout", "HEAD", "--", path)
            except Exception:
                pass
        res.rolled_back.append(path)
    if not res.ok:
        run.tracer.log(event_detail="permission_breach", role=role,
                       breaches=res.breaches, rolled_back=res.rolled_back)
    return res
