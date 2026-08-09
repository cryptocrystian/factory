"""Gates audit an agent's claims against reality (I8). A green gate says WHAT it verified,
not just that it passed. Gate failures return to the same OMP session as corrections
(bounded by retries); a permission breach is different — that aborts (permissions.py)."""
from __future__ import annotations

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
    committed = set()
    checks = []
    for f in claimed:
        on_disk = (run.workspace / f).exists()
        checks.append({"item": f, "ok": on_disk, "note": "present" if on_disk else "claimed but absent"})
    passed = all(c["ok"] for c in checks) if checks else True
    return GateReport(gate="diff_matches_claims", passed=passed, checks=checks,
                      evidence=f"{len(claimed)} files claimed")


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
    """Factory: a known command that must exit 0 (typecheck, check:tokens, test:unit, next build)."""
    def _gate(env, run) -> GateReport:
        p = subprocess.run(command, shell=True, cwd=str(run.workspace),
                           capture_output=True, text=True, timeout=600)
        tail = (p.stdout + p.stderr)[-1000:]
        return GateReport(gate=name, passed=p.returncode == 0,
                          checks=[{"item": command, "ok": p.returncode == 0}],
                          evidence=tail if p.returncode != 0 else f"{name} exit 0")
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
