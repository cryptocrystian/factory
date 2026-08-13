#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""K2 self-test: write-enforcement + gates, in a throwaway git repo (no spend)."""
import subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config, permissions as perm, gates
import envelopes as E
config.RUNS_DIR = Path(tempfile.mkdtemp())        # isolate worktrees/run dirs from the real factory
from session import Run

ok = True
def check(name, cond, detail=""):
    global ok; ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

# ---- permitted() matrix (pure) ----
print("permitted() matrix:")
check("builder may write source", perm.permitted("lib/x.ts", "builder"))
check("builder may NOT write canon (protected)", not perm.permitted("canon/y.md", "builder"))
check("builder may NOT write migrations", not perm.permitted("supabase/migrations/0001.sql", "builder"))
check("test-author may write tests", perm.permitted("tests/unit/a.spec.ts", "test-author"))
check("test-author may NOT write source (the P0 breach)", not perm.permitted("lib/valuation/compute.ts", "test-author"))
check("reviewer (read-only) may write nothing", not perm.permitted("lib/x.ts", "reviewer"))

# ---- enforce() rolls back a real out-of-grant write ----
print("enforce() rollback in a temp repo:")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo)
    (repo / "lib").mkdir(); (repo / "tests").mkdir()
    (repo / "lib" / "compute.ts").write_text("export const v = 1;\n")
    subprocess.run(["git", "add", "-A"], cwd=repo)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base"], cwd=repo)

    run = Run(repo, lane="selftest", target="k2", run_id="selftest-k2")
    ws = Path(run.workspace)                        # the run operates in the isolated worktree
    (ws / "tests").mkdir(exist_ok=True)             # empty dirs aren't committed, so ensure it exists

    # test-author illegally edits source + legally adds a test (in the worktree)
    (ws / "lib" / "compute.ts").write_text("export const v = 999; // tampered\n")
    (ws / "tests" / "a.spec.ts").write_text("test('x', () => {})\n")
    res = perm.enforce(run, "test-author")
    check("breach detected", not res.ok, f"breaches={res.breaches}")
    check("source change rolled back", (ws / "lib" / "compute.ts").read_text().strip() == "export const v = 1;")
    check("legal test file kept", (ws / "tests" / "a.spec.ts").exists())

    # builder writing source is allowed (no breach)
    (ws / "lib" / "compute.ts").write_text("export const v = 2;\n")
    res2 = perm.enforce(run, "builder")
    check("builder source write allowed", res2.ok and (ws / "lib" / "compute.ts").read_text().strip().endswith("v = 2;"))

    # ---- cmd_gate (runs in the worktree) ----
    print("cmd_gate:")
    gp = gates.cmd_gate("true-gate", "true")(None, run)
    check("passing command -> passed", gp.passed)
    gf = gates.cmd_gate("false-gate", "false")(None, run)
    check("failing command -> not passed", not gf.passed)

    run._iso.destroy(keep_branch=False)            # clean up the worktree

# ---- verdict_consistent (pure) ----
print("verdict_consistent:")
bad = E.ReviewOutput(status="success", approved=True, blocking=["x"],
                     findings=[E.ReviewFinding(severity="blocking", note="x")])
good = E.ReviewOutput(status="success", approved=True, blocking=[], findings=[])
rej = E.ReviewOutput(status="success", approved=False, blocking=["y"],
                     findings=[E.ReviewFinding(severity="blocking", note="y")])
check("approved+blocking -> inconsistent", not gates.verdict_consistent(bad, None).passed)
check("approved+clean -> consistent", gates.verdict_consistent(good, None).passed)
check("rejected+named problem -> consistent", gates.verdict_consistent(rej, None).passed)

print("\nK2 gates + write-enforcement self-test:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
