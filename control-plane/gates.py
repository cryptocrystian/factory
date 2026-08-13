"""Gates audit an agent's claims against reality (I8). A green gate says WHAT it verified,
not just that it passed. Gate failures return to the same OMP session as corrections
(bounded by retries); a permission breach is different — that aborts (permissions.py)."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from envelopes import GateReport, EnvelopeBase


def artifacts_exist(env: EnvelopeBase, run) -> GateReport:
    arts = getattr(env, "artifacts", None) or getattr(env, "changed_files", None) or []
    checks = []
    for a in arts:
        p = run.dir / a                          # plans live in the run dir; source in the workspace
        if not p.exists():
            p = run.workspace / a
        checks.append({"item": a, "ok": p.exists(),
                       "note": f"{p.stat().st_size}B" if p.exists() else "missing"})
    passed = all(c["ok"] for c in checks) if checks else False
    return GateReport(gate="artifacts_exist", passed=passed, checks=checks,
                      evidence=f"{sum(c['ok'] for c in checks)}/{len(checks)} artifacts present")


def files_non_empty(env: EnvelopeBase, run) -> GateReport:
    arts = getattr(env, "artifacts", None) or getattr(env, "changed_files", None) or []
    checks = [{"item": a, "ok": (run.workspace / a).exists() and (run.workspace / a).stat().st_size > 0}
              for a in arts]
    passed = all(c["ok"] for c in checks) if checks else False
    return GateReport(gate="files_non_empty", passed=passed, checks=checks)


def diff_matches_claims(env: EnvelopeBase, run) -> GateReport:
    """Every file the agent claims it changed actually differs on disk (tracked or untracked)."""
    claimed = getattr(env, "changed_files", []) or []
    changed = {c[3:] for c in run.git("status", "--porcelain=v1", "-z").split("\0") if len(c) > 3}
    base = getattr(run, "_base", None)
    if base:                                    # include changes already committed this run (phase commits)
        try:
            for f in run.git("diff", "--name-only", base, "HEAD").splitlines():
                if f.strip():
                    changed.add(f.strip())
        except Exception:
            pass
    checks = [{"item": f, "ok": f in changed,
               "note": "changed" if f in changed else "claimed but unchanged"} for f in claimed]
    passed = all(c["ok"] for c in checks) if checks else True
    return GateReport(gate="diff_matches_claims", passed=passed, checks=checks,
                      evidence=f"{sum(c['ok'] for c in checks)}/{len(claimed)} claimed files actually changed")


def verdict_consistent(env, run) -> GateReport:
    """A review's verdict must agree with its own findings (judges nothing about the code)."""
    approved = getattr(env, "approved", None)
    blocking = getattr(env, "blocking", []) or []
    findings = getattr(env, "findings", []) or []
    has_blocking_finding = any(getattr(f, "severity", "") == "blocking" for f in findings)
    checks = [
        {"item": "approved vs blocking list", "ok": not (approved and blocking)},
        {"item": "approved vs blocking findings", "ok": not (approved and has_blocking_finding)},
        {"item": "rejection names a problem", "ok": bool(approved) or bool(blocking or findings)},
    ]
    passed = all(c["ok"] for c in checks)
    return GateReport(gate="verdict_consistent", passed=passed, checks=checks,
                      evidence=f"approved={approved}, {len(blocking)} blocking")


def cmd_gate(name: str, command: str):
    """Factory: a known command that must exit 0 (typecheck, check:tokens, test:unit, next build).
    Runs in its own session so a hung/backgrounding command's whole process tree is reaped on timeout."""
    def _gate(env, run) -> GateReport:
        proc = subprocess.Popen(command, shell=True, cwd=str(run.workspace),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait(timeout=10)
            return GateReport(gate=name, passed=False,
                              checks=[{"item": command, "ok": False}],
                              evidence=f"{name} timed out after 600s (process tree killed)")
        tail = (out or "")[-1000:]
        return GateReport(gate=name, passed=proc.returncode == 0,
                          checks=[{"item": command, "ok": proc.returncode == 0}],
                          evidence=tail if proc.returncode != 0 else f"{name} exit 0")
    return _gate


def impeccable_gate(version: str, paths: str):
    """Anti-slop design gate (I8): the Impeccable detector must find no non-advisory findings.

    Deterministic and LLM-free — a peer of typecheck/check:tokens, run in the build L0 tier. The
    detector exits 2 when a warning/error-severity slop pattern is present (overused font, muddy
    color pair, bounce easing, off-token spacing…) and 0 when clean; the human-readable findings
    become the gate evidence and route back to the builder like any failing L0 gate. Missing scan
    dirs are skipped (exit 0), so `paths` may list more than a given repo has.

    Opt-in per repo (registry.yml `design.enabled`). Enable ONLY once the repo has a ratified
    DESIGN.md + .impeccable/config.json declaring its fonts/tokens — otherwise the brand font reads
    as an 'overused-font' finding and the gate false-fails. Version is pinned for reproducibility."""
    ver = f"@{version}" if version else ""
    return cmd_gate("design:slop", f"npx --yes impeccable{ver} detect {paths}")
