#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""K7 self-test: branch isolation + gated merge, in throwaway repos (no spend)."""
import subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
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

print("rejected run leaves main untouched:")
with tempfile.TemporaryDirectory() as td:
    repo = fresh_repo(td); base = head(repo)
    run = Run(repo, "selftest", "reject", run_id="k7-reject")
    check("on work branch", branch(repo).startswith("factory/"), branch(repo))
    (repo / "f.txt").write_text("changed by run\n")
    run.commit("wip change")
    run.finish(accepted=False)
    check("back on main", branch(repo) == "main")
    check("main HEAD unchanged (not merged)", head(repo) == base)
    check("work branch preserved for inspection",
          "factory/k7-reject" in subprocess.run(["git","branch"],cwd=repo,capture_output=True,text=True).stdout)
    check("working tree restored to base", (repo/"f.txt").read_text() == "base\n")

print("accepted run merges to main:")
with tempfile.TemporaryDirectory() as td:
    repo = fresh_repo(td); base = head(repo)
    run = Run(repo, "selftest", "accept", run_id="k7-accept")
    (repo / "f.txt").write_text("accepted change\n")
    run.commit("good change")
    run.finish(accepted=True)
    check("back on main", branch(repo) == "main")
    check("main advanced (merged)", head(repo) != base)
    check("change is on main", (repo/"f.txt").read_text() == "accepted change\n")

print("\nK7 isolation self-test:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
