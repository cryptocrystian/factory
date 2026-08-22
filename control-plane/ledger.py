"""The acceptance ledger — agent-declared criteria that CODE decides.

Ported from disler-adjacent practice via the `unlazy` skill (Leonxlnx/unlazy), whose central claim
is one this factory already lives by from the other direction: *prose cannot enforce prose.* Our
gates are factory-authored — code we wrote, judging the agent. This adds the mirror: the PLANNER
declares, before any code is written, what would prove the work done, as runnable CHECK/EXPECT
pairs. Code runs them. A box flips only when the command's output actually contains what was
promised, and the deciding line is recorded as evidence.

WHY IT EARNS ITS PLACE HERE. Our expensive resource is judge cycles — 78 API requests a run, and a
weekly subscription that just hit 100%. A machine-checkable criterion that fails should cost a
shell command, not a review cycle. The ledger runs BEFORE the reviewer and hands its failures to
the builder, so the judge is spent on questions only judgment can answer.

WHAT IT IS NOT. It is not acceptance. The reviewer remains the authority and the L0 gates remain
mandatory; a full ledger is evidence offered to the judge, never a substitute for it. An agent that
could green its own build by writing easy gates would be marking its own homework — which is the
exact failure the cross-family reviewer exists to prevent.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_GATES = 12                 # a ledger, not a test suite
CHECK_TIMEOUT_S = 180

# Commands a declared gate may never run. Gates are agent-authored and execute in the run's
# worktree, so the ledger refuses anything that reaches outside it, mutates history, or fetches
# and executes code. A refused gate is reported, never silently skipped.
_FORBIDDEN = (
    r"\brm\s+-rf\s+/", r"\bsudo\b", r"\bgit\s+push\b", r"\bgit\s+reset\s+--hard\b",
    r"\bcurl\b[^|]*\|\s*(ba)?sh", r"\bwget\b[^|]*\|\s*(ba)?sh", r">\s*/dev/sd", r"\bmkfs\b",
    r"\bshutdown\b", r"\breboot\b", r"\bchmod\s+777\s+/", r"\.\./\.\./\.\.",
)


@dataclass
class GateResult:
    id: str
    description: str
    check: str
    expect: str
    passed: bool = False
    evidence: str = ""
    refused: str = ""


@dataclass
class LedgerReport:
    results: list[GateResult] = field(default_factory=list)

    @property
    def met(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    def unmet(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]

    def findings(self) -> list[str]:
        """Unmet gates as builder-facing findings — the agent's own promise, unkept."""
        out = []
        for r in self.unmet():
            if r.refused:
                out.append(f"[{r.id}] gate refused ({r.refused}): {r.description}")
            else:
                out.append(f"[{r.id}] {r.description} — `{r.check}` did not produce "
                           f"'{r.expect}'. Observed: {r.evidence[:200] or '(no output)'}")
        return out

    def markdown(self) -> str:
        lines = ["# Acceptance ledger", "",
                 f"**{self.met}/{self.total} gates met.** Declared by the planner before the build;",
                 "each box flipped by running its CHECK, never by assertion.", ""]
        for r in self.results:
            box = "x" if r.passed else " "
            lines += [f"- [{box}] {r.id}: {r.description}",
                      f"      CHECK: {r.check}",
                      f"      EXPECT: {r.expect}",
                      f"      EVIDENCE: {(r.refused or r.evidence or 'pending')[:300]}"]
        return "\n".join(lines)


def _refusal(cmd: str) -> str:
    for pat in _FORBIDDEN:
        if re.search(pat, cmd, re.IGNORECASE):
            return f"matches forbidden pattern {pat!r}"
    return ""


def run(gates, workspace: Path) -> LedgerReport:
    """Execute each declared gate in the run's workspace and decide it by output."""
    rep = LedgerReport()
    for g in list(gates or [])[:MAX_GATES]:
        gid = str(getattr(g, "id", "") or getattr(g, "ref", "") or f"G{len(rep.results)+1}")
        desc = str(getattr(g, "description", "") or "")
        check = str(getattr(g, "check", "") or "").strip()
        expect = str(getattr(g, "expect", "") or "").strip()
        res = GateResult(id=gid, description=desc, check=check, expect=expect)
        if not check or not expect:
            res.refused = "gate declared without a CHECK or EXPECT"
            rep.results.append(res)
            continue
        bad = _refusal(check)
        if bad:
            res.refused = bad
            rep.results.append(res)
            continue
        try:
            p = subprocess.run(["bash", "-lc", check], cwd=str(workspace), capture_output=True,
                               text=True, timeout=CHECK_TIMEOUT_S)
            out = (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            res.refused = f"CHECK exceeded {CHECK_TIMEOUT_S}s"
            rep.results.append(res)
            continue
        except OSError as ex:
            res.refused = f"CHECK could not run: {ex}"
            rep.results.append(res)
            continue
        res.passed = expect.lower() in out.lower()
        # The deciding line, not the whole log — evidence should be readable at a glance.
        for line in out.splitlines():
            if expect.lower() in line.lower():
                res.evidence = line.strip()
                break
        else:
            res.evidence = (out.strip().splitlines() or [""])[-1][:300]
        rep.results.append(res)
    return rep
