#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""K7 self-test: the Isolation port (git-worktree) + gated merge, in throwaway repos (no spend).

New guarantee vs. the old in-place-branch model: the ORIGIN repo's working tree is never touched by a
run — the run operates in a separate worktree. Accepted work merges back to origin/base; rejected work
leaves origin untouched and keeps the branch for inspection; the worktree is always destroyed."""
import subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config
config.RUNS_DIR = Path(tempfile.mkdtemp())        # isolate worktrees + run dirs from the real factory
from session import Run

ok = True
def check(name, cond, detail=""):
    global ok; ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

def fresh_repo(td):
    repo = Path(td)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo)
    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base"], cwd=repo)
    return repo

def head(repo): return subprocess.run(["git","rev-parse","HEAD"],cwd=repo,capture_output=True,text=True).stdout.strip()
def branch(repo): return subprocess.run(["git","rev-parse","--abbrev-ref","HEAD"],cwd=repo,capture_output=True,text=True).stdout.strip()

print("rejected run leaves origin untouched:")
with tempfile.TemporaryDirectory() as td:
    repo = fresh_repo(td); base = head(repo)
    run = Run(repo, "selftest", "reject", run_id="k7-reject")
    wt = Path(run.workspace)
    check("origin stays on main (not switched)", branch(repo) == "main", branch(repo))
    check("run operates in a separate worktree", wt != repo and wt.exists())
    check("worktree is on a factory/ branch", branch(wt).startswith("factory/"), branch(wt))
    (wt / "f.txt").write_text("changed by run\n")          # mutate ONLY the worktree
    check("origin tree untouched while run mutates worktree", (repo/"f.txt").read_text() == "base\n")
    run.commit("wip change")
    result = run.finish(accepted=False)
    check("finish returns False", result is False)
    check("origin HEAD unchanged (not merged)", head(repo) == base)
    check("origin tree still base", (repo/"f.txt").read_text() == "base\n")
    check("work branch preserved for inspection",
          "factory/k7-reject" in subprocess.run(["git","branch"],cwd=repo,capture_output=True,text=True).stdout)
    check("worktree destroyed", not wt.exists())

print("accepted run merges to origin main:")
with tempfile.TemporaryDirectory() as td:
    repo = fresh_repo(td); base = head(repo)
    run = Run(repo, "selftest", "accept", run_id="k7-accept")
    wt = Path(run.workspace)
    (wt / "f.txt").write_text("accepted change\n")
    run.commit("good change")
    result = run.finish(accepted=True)
    check("finish returns True", result is True)
    check("origin still on main", branch(repo) == "main")
    check("origin main advanced (merged)", head(repo) != base)
    check("change landed on origin main", (repo/"f.txt").read_text() == "accepted change\n")
    check("worktree destroyed", not wt.exists())

print("accepted but base advanced under the run -> refused (I11), downgraded to False:")
with tempfile.TemporaryDirectory() as td:
    repo = fresh_repo(td); base = head(repo)
    run = Run(repo, "selftest", "i11", run_id="k7-i11")
    wt = Path(run.workspace)
    (wt / "g.txt").write_text("work\n"); run.commit("work on branch")
    # a concurrent actor advances origin/main after the run pinned its base:
    (repo / "c.txt").write_text("concurrent\n")
    subprocess.run(["git","add","-A"],cwd=repo); subprocess.run(["git","-c","commit.gpgsign=false","commit","-q","-m","concurrent"],cwd=repo)
    result = run.finish(accepted=True)
    g_on_main = subprocess.run(["git","cat-file","-e","main:g.txt"],cwd=repo,capture_output=True).returncode == 0
    check("finish returns False (refused)", result is False)
    check("run work NOT on origin main", not g_on_main)

print("\nK7 isolation self-test:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
