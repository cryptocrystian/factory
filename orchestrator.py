#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6","pydantic>=2"]
# ///
"""The orchestrator — the out-of-loop dispatcher (Rev4 §9: intake → route → dispatch → collect → escalate).

Reads a backlog of work items with dependencies and drains everything it can do autonomously:
pick the next READY item (deps satisfied) → run the right lane → on accept, advance and unblock
dependents; on escalation, write an evidence bundle to the decisions queue and move to the next
INDEPENDENT item (batched, never stop-the-line). It stops only when all that's left is blocked or
waiting on your ruling — the human decides, the human doesn't dispatch.

    uv run orchestrator.py [--dry-run] [--once]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import yaml

FACTORY_ROOT = Path(__file__).resolve().parent
BACKLOG = FACTORY_ROOT / "backlog.yml"
ESC_DIR = FACTORY_ROOT / "runs" / "escalations"
CP = FACTORY_ROOT / "control-plane"
sys.path.insert(0, str(CP))
import obsdb
import notifier as notifier_mod

DONE = {"accepted", "done"}
LANE = str(FACTORY_ROOT / "lanes" / "feature.py")


# ----------------------------------------------------------------------------- backlog
def load():
    return yaml.safe_load(BACKLOG.read_text()) or {"items": []}


def save(data):
    BACKLOG.write_text("# Factory backlog — the orchestrator's work queue. Edit status to re-open an item.\n"
                       + yaml.safe_dump(data, sort_keys=False))


def repo_path(name: str) -> str:
    reg = yaml.safe_load((FACTORY_ROOT / "registry.yml").read_text()) or {}
    spec = (reg.get("repos") or {}).get(name) or {}
    return str(Path(spec.get("path", name)).expanduser())


def ready(items):
    """Dispatchable = ready, or blocked with every dependency now accepted (blocked→ready transition).
    Decisions (awaiting_human) and escalated items are never auto-dispatched — they wait on you."""
    done = {i["id"] for i in items if i.get("status") in DONE}
    return [i for i in items
            if i.get("status") in ("ready", "blocked")
            and all(d in done for d in (i.get("depends_on") or []))]


# ----------------------------------------------------------------------------- dispatch
def _cmd(item):
    repo = repo_path(item["repo"])
    if item["kind"] == "journey":
        return ["uv", "run", LANE, "--repo", repo, "--journey", item["journey"]]
    if item["kind"] == "foundation":
        return ["uv", "run", LANE, "--repo", repo, "--journey", item["id"],
                "--foundation", str(FACTORY_ROOT / item["brief"])]
    if item["kind"] == "remediation":
        return ["uv", "run", LANE, "--repo", repo, "--journey", item["journey"],
                "--remediate", str(FACTORY_ROOT / item["findings"])]
    raise ValueError(f"cannot dispatch kind={item['kind']}")


def _latest_run(target):
    conn = obsdb.connect()
    try:
        for r in obsdb.list_runs(conn):
            if r.get("target") == target:
                return r
    finally:
        conn.close()
    return None


def _blocking(run_id):
    conn = obsdb.connect()
    try:
        run = obsdb.get_run(conn, run_id)
    finally:
        conn.close()
    if not run:
        return []
    import json
    out = []
    for e in run["events"]:
        d = json.loads(e["detail"]) if e.get("detail") else {}
        if d.get("event_detail") == "review_verdict" and d.get("blocking"):
            out = d["blocking"]  # keep the last review's blocking
    return out


def dispatch(item, dry_run):
    """Returns (accepted: bool, run_id: str|None, blocking: list[str])."""
    target = item["id"] if item["kind"] == "foundation" else item.get("journey", item["id"])
    if dry_run:
        acc = item.get("dry_outcome", "accepted") == "accepted"
        return acc, f"dry-{item['id']}", ([] if acc else [f"(dry) simulated escalation for {item['id']}"])
    proc = subprocess.run(_cmd(item), cwd=str(FACTORY_ROOT))
    run = _latest_run(target)
    run_id = run["run_id"] if run else None
    accepted = proc.returncode == 0
    return accepted, run_id, ([] if accepted else _blocking(run_id) if run_id else ["run produced no verdict"])


def escalate(item, run_id, blocking):
    ESC_DIR.mkdir(parents=True, exist_ok=True)
    p = ESC_DIR / f"{item['id']}.md"
    lines = [f"# Escalation — {item['id']}", "",
             f"**Item:** {item.get('note','')}",
             f"**Kind:** {item['kind']} · **Repo:** {item.get('repo','—')} · **Run:** `{run_id}`", "",
             "The factory ran this item but did not reach acceptance. It refused to fake a green and is",
             "asking for your ruling. Blocking findings from the independent reviewer:", ""]
    lines += [f"- {b}" for b in (blocking or ["(no structured findings captured — see the run in the observatory)"])]
    lines += ["", "## To resolve", "1. Rule the finding (usually a DEC in canon, mirroring DEC-052/055/056).",
              f"2. Set this item's `status: ready` in `backlog.yml` (or add a remediation item citing this run).",
              "3. Re-run the orchestrator — it will pick it back up.", "",
              f"Inspect the full run at http://localhost:7788 → {run_id}"]
    p.write_text("\n".join(lines))
    return p


# ----------------------------------------------------------------------------- loop
def run_orchestrator(dry_run=False, once=False):
    data = load()
    items = data["items"]
    acted = []
    persist = not dry_run                        # a dry run never mutates the real backlog
    # A dry run posts nothing externally; a real run routes lifecycle events through
    # the Notifier port (Buzz by default; file-only if unconfigured/unreachable).
    nt = notifier_mod.NullNotifier() if dry_run else notifier_mod.get_notifier()
    while True:
        rd = ready(items)
        if not rd:
            break
        item = rd[0]
        print(f"▶ dispatch  {item['id']}  ({item['kind']})")
        item["status"] = "in_progress"
        if persist: save(data)
        accepted, run_id, blocking = dispatch(item, dry_run)
        if accepted:
            item["status"] = "accepted"; item["run_id"] = run_id
            print(f"  ✓ accepted  {item['id']}  → unblocks dependents")
            acted.append((item["id"], "accepted", None))
            nt.accepted(project=item.get("repo", "—"), item_id=item["id"],
                        kind=item["kind"], note=item.get("note", ""), run_id=run_id)
        else:
            item["status"] = "escalated"; item["run_id"] = run_id
            if persist:
                bundle = escalate(item, run_id, blocking)
                print(f"  ⚑ escalated {item['id']}  → {bundle.relative_to(FACTORY_ROOT)}")
                acted.append((item["id"], "escalated", str(bundle)))
                nt.escalation(project=item.get("repo", "—"), item_id=item["id"],
                              kind=item["kind"], note=item.get("note", ""),
                              run_id=run_id, blocking=blocking)
            else:
                print(f"  ⚑ escalated {item['id']}  (dry — no bundle written)")
                acted.append((item["id"], "escalated", None))
        if persist:
            save(data)
        if once:
            break
    report(items, acted, dry_run)


def report(items, acted, dry_run):
    done = {i["id"] for i in items if i.get("status") in DONE}
    print("\n" + ("── DRY RUN " if dry_run else "── ") + "orchestrator report " + "─" * 30)
    for i in items:
        st = i.get("status")
        icon = {"accepted": "✓", "done": "✓", "escalated": "⚑", "ready": "▷",
                "blocked": "⋯", "awaiting_human": "☐", "in_progress": "…"}.get(st, "·")
        dep = ""
        unmet = [d for d in (i.get("depends_on") or []) if d not in done]
        if unmet:
            dep = f"  (waiting on: {', '.join(unmet)})"
        print(f"  {icon} {i['id']:22s} {str(st):13s} {i.get('note','')}{dep}")
    esc = [a for a in acted if a[1] == "escalated"]
    aw = [i for i in items if i.get("status") == "awaiting_human"]
    print("─" * 52)
    if esc:
        print(f"⚑ {len(esc)} escalation(s) need your ruling — see runs/escalations/")
    if aw:
        print(f"☐ {len(aw)} decision(s) awaiting you (launch gates): " + ", ".join(i["id"] for i in aw))
    if not esc and not aw and not ready(items):
        print("nothing left the factory can do autonomously — backlog drained.")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="simulate dispatch outcomes (no lanes run)")
    ap.add_argument("--once", action="store_true", help="dispatch a single ready item, then stop")
    a = ap.parse_args(argv)
    if not BACKLOG.exists():
        print(f"no backlog at {BACKLOG}"); sys.exit(1)
    run_orchestrator(dry_run=a.dry_run, once=a.once)


if __name__ == "__main__":
    main(sys.argv[1:])
