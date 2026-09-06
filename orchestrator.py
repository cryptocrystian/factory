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
import os
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
import readiness
import statefile
import notifier as notifier_mod
import canon
import omp

DONE = {"accepted", "done"}
MAX_CYCLE_FAILURES = 5     # consecutive failing cycles before the daemon gives up and exits loudly
JUDGE_PROBE_TTL = 600      # seconds a healthy judge-provider probe is trusted before re-checking
LANE = str(FACTORY_ROOT / "lanes" / "feature.py")


# ----------------------------------------------------------------------------- backlog
def load():
    return yaml.safe_load(BACKLOG.read_text()) or {"items": []}


BACKLOG_HEADER = "# Factory backlog — the orchestrator's work queue. Edit status to re-open an item.\n"


def save_fields(changes: dict):
    """Write ONLY the fields this process changed, onto what is on disk NOW.

    The daemon used to rewrite the whole backlog from a copy read 20 seconds earlier, which erased
    any ruling approved in the observatory during that window — the item stayed escalated and was
    never re-picked. Applying just our own deltas under a lock means the two writers compose."""
    if not changes:
        return

    def mutate(data):
        by_id = {i["id"]: i for i in data.get("items", [])}
        for iid, fields in changes.items():
            if iid in by_id:
                by_id[iid].update(fields)
        return data

    statefile.update(BACKLOG, mutate, header=BACKLOG_HEADER, default={"items": []})


def save(data):
    BACKLOG.write_text("# Factory backlog — the orchestrator's work queue. Edit status to re-open an item.\n"
                       + yaml.safe_dump(data, sort_keys=False))


def repo_path(name: str) -> str:
    reg = yaml.safe_load((FACTORY_ROOT / "registry.yml").read_text()) or {}
    spec = (reg.get("repos") or {}).get(name) or {}
    return str(Path(spec.get("path", name)).expanduser())


def ready(items):
    """Dispatchable = ready, or blocked with every dependency now accepted (blocked→ready transition),
    or held on infrastructure (a provider outage is not a verdict, so it must not need a human to
    clear it — the JudgeGate preflight already refuses to dispatch while no judge route is up).
    Decisions (awaiting_human) and escalated items are never auto-dispatched — they wait on you."""
    done = {i["id"] for i in items if i.get("status") in DONE}
    return [i for i in items
            if i.get("status") in ("ready", "blocked", "infra_hold")
            and all(d in done for d in (i.get("depends_on") or []))]


# ----------------------------------------------------------------------------- dispatch
def delivery_state(repo: str) -> tuple[bool, str]:
    """Is this repo's work actually SHIPPED — local base branch equal to its remote?

    Table stakes the orchestrator was not doing: JRN-S4's accepted merge sat unpushed on the box for
    a day while the handoff claimed GitHub was the origin of truth. Dispatching onto an out-of-sync
    repo also guarantees the next merge either strands too or loses the base-moved check (I11)."""
    try:
        path = Path(repo_path(repo))
        branch = _git_out(path, "rev-parse", "--abbrev-ref", "HEAD")
        _git_out(path, "fetch", "--quiet", "origin", branch)
        counts = _git_out(path, "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD")
        behind, ahead = (int(x) for x in counts.split())
    except Exception as ex:
        return True, f"delivery state unknown ({type(ex).__name__}) — proceeding"
    if ahead and behind:
        return False, f"{repo} has DIVERGED from origin/{branch} (+{ahead}/-{behind}) — reconcile before dispatch"
    if ahead:
        return False, f"{repo} is {ahead} commit(s) ahead of origin/{branch} — accepted work is NOT shipped"
    if behind:
        return False, f"{repo} is {behind} commit(s) behind origin/{branch} — runs would start from a stale base"
    return True, f"{repo} in sync with origin/{branch}"


def _git_out(path: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True,
                          check=True, timeout=120).stdout.strip()


def claim_migration_number(item, held_claims) -> str:
    """Reserve the next migration number for THIS run, accounting for runs already in flight.

    Migration filenames are a shared, ordered resource that no canon binding covers. The
    decomposition gate correctly ran JRN-S4 and JRN-B1 in parallel — their `Touches` sets are
    disjoint — but both then authored `0012`, because each read the same `main` and each took
    "the next number". The gate schedules on entities; the sequence needs claiming too.

    Claims are per dispatch and held for the life of the run, so two concurrent journeys get
    0012 and 0013 rather than 0012 twice."""
    try:
        migdir = Path(repo_path(item["repo"])) / "supabase" / "migrations"
        used = {int(f.name[:4]) for f in migdir.glob("[0-9][0-9][0-9][0-9]_*.sql")} if migdir.is_dir() else set()
    except Exception:
        used = set()
    used |= {int(n) for n in held_claims if str(n).isdigit()}
    return f"{(max(used) + 1) if used else 1:04d}"



# Reasons that mean the work was NEVER JUDGED. Only these may be resumed: a build the reviewer
# rejected is tainted and belongs in the fix loop against its findings, not back on the bench.
_UNJUDGED = ("unavailable (infra)", "provider failure", "killed mid-flight")


def resumable_branch(item) -> str | None:
    """The work branch of this target's most recent UNJUDGED run, if it still holds commits.

    A run that dies on a provider outage leaves a finished build on its branch. Rebuilding it from
    base to reach the same exhausted reviewer wastes the whole plan+build — which is how 98
    abandoned branches accumulated, JRN-B1 alone rebuilt from scratch eight times. Resuming turns a
    provider outage into the cost of a review rather than the cost of a journey."""
    target = _target_of(item)
    repo = Path(repo_path(item["repo"]))
    conn = obsdb.connect()
    try:
        for r in obsdb.list_runs(conn, limit=120):
            if (r.get("target") or "").lower() != target.lower():
                continue
            if r.get("accepted"):
                return None                    # already landed; nothing to resume
            reason = str(r.get("reason") or "")
            if not any(k in reason.lower() for k in _UNJUDGED):
                return None                    # newest outcome was a JUDGEMENT — do not resume it
            branch = f"factory/{r['run_id']}"
            try:
                _git_out(repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
                ahead = _git_out(repo, "rev-list", "--count", f"main..{branch}").strip()
            except Exception:
                continue                       # branch pruned; look further back
            if ahead.isdigit() and int(ahead) > 0:
                return branch
        return None
    finally:
        conn.close()


def _cmd(item):
    repo = repo_path(item["repo"])
    if item["kind"] == "journey":
        cmd = ["uv", "run", LANE, "--repo", repo, "--journey", item["journey"]]
        rb = resumable_branch(item)
        if rb:
            cmd += ["--resume", rb]
        return cmd
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


DECISIONS = FACTORY_ROOT / "runs" / "decisions.yml"
REWORK_DIR = FACTORY_ROOT / "runs" / "rework"
MAX_REWORK = 2      # rework attempts before unclosed findings are genuinely the owner's


def _queue_decision(item, run_id, blocking, human_brief=None):
    """Put the escalation in the DECISIONS QUEUE, which is what the observatory serves and what the
    owner rules from — approving there advances the backlog automatically.

    This module's own docstring has promised "write an evidence bundle to the decisions queue"
    since it was written, and it never did: escalations only ever became markdown files nobody was
    told about, so the approvals half of the observatory sat empty and every ruling had to come
    through hand-edited YAML."""
    did = f"{item['id']}-{run_id}" if run_id else item["id"]
    hb = human_brief or {}
    record = {
        "id": did,
        "backlog_id": item["id"],
        "kind": "decision" if hb.get("question") else "escalation",
        "repo": item.get("repo", "—"),
        "run_id": run_id,
        "item_note": item.get("note", ""),
        "finding": hb.get("question") or "; ".join(blocking or []) or "did not reach acceptance",
        "why": hb.get("why", ""),
        "options": hb.get("options", []),
        "recommendation": hb.get("recommendation", ""),
        "blocking": list(blocking or []),
        "status": "open",
    }
    def _add(cur):
        cur.setdefault("decisions", [])
        if any(d.get("id") == did for d in cur["decisions"]):
            return cur                       # another writer queued it first
        cur["decisions"].append(record)
        return cur

    statefile.update(DECISIONS, _add,
                     header="# Decisions queue — escalations + ratifications awaiting the human.\n",
                     default={"decisions": []})


def escalate(item, run_id, blocking, human_brief=None, operational=False):
    """Write the escalation bundle. `operational=True` means INFRASTRUCTURE, not judgment.

    A rate-limited provider is not a thing a human can rule. Queuing it as a decision put
    "repeated transient infra failure (model overloaded / no envelope)" in front of the owner as
    though it were a ruling, and parked every dependent behind it — JRN-B1 held b4/m1/f1 (and s5,
    t1, t2, n1, n2 behind those) on a condition that had already cleared by itself. Operational
    holds notify, but they never enter the decisions queue and they reopen on their own."""
    if not operational:
        _queue_decision(item, run_id, blocking, human_brief)
    ESC_DIR.mkdir(parents=True, exist_ok=True)
    p = ESC_DIR / f"{item['id']}.md"
    if operational:
        p.write_text("\n".join([
            f"# Infrastructure hold — {item['id']}", "",
            f"**Item:** {item.get('note','')}",
            f"**Kind:** {item['kind']} · **Repo:** {item.get('repo','—')} · **Run:** `{run_id}`", "",
            "**There is no ruling to make here.** The run did not fail on its merits — a provider was",
            "unavailable (rate limited, overloaded, or returning no envelope) for every retry.", "",
            "Cause:", *[f"- {b}" for b in (blocking or ["provider unavailable"])], "",
            "## What happens next",
            "This item is on an infrastructure hold, not an escalation. The daemon reopens it",
            "automatically once a judge route is healthy again; no action is needed from you.",
            "Check provider headroom with `python3 control-plane/quota.py`.", "",
            f"Run detail: {run_id}"]))
        return p
    lines = [f"# Escalation — {item['id']}", "",
             f"**Item:** {item.get('note','')}",
             f"**Kind:** {item['kind']} · **Repo:** {item.get('repo','—')} · **Run:** `{run_id}`", "",
             "The factory ran this item but did not reach acceptance. It refused to fake a green and is",
             "asking for your ruling. Blocking findings from the independent reviewer:", ""]
    lines += [f"- {b}" for b in (blocking or ["(no structured findings captured — see the run in the observatory)"])]
    lines += ["", "## To resolve", "1. Rule the finding (usually a DEC in canon, mirroring DEC-052/055/056).",
              f"2. Set this item's `status: ready` in `backlog.yml` (or add a remediation item citing this run).",
              "3. Re-run the orchestrator — it will pick it back up.", "",
              f"Rule it in the observatory (Decisions tab) — approving there re-opens this item",
              f"automatically. Run detail: {run_id}"]
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


# ----------------------------------------------------------------------------- continuous parallel daemon
def _verdict(target):
    """Read the latest run's TRUE finish for `target`: (accepted, merged, run_id, blocking,
    transient, cooldown_s).
    `merged` distinguishes a real acceptance from a build that was accepted but lost the merge race
    on a moving base (I11) — the latter is re-queued, not escalated. `rework` carries findings the
    architect could not close: unfinished engineering, which is re-dispatched as remediation rather
    than escalated, because no ruling can settle a defect."""
    import json
    run = _latest_run(target)
    if not run:
        return False, False, None, ["run produced no verdict"], False, 0.0, None, {}, None, []
    run_id = run["run_id"]
    conn = obsdb.connect()
    try:
        r = obsdb.get_run(conn, run_id)
    finally:
        conn.close()
    accepted = merged = transient = False
    rework: list[str] = []
    cooldown = 0.0
    cost = None
    brief = {}
    pushed = None
    for e in (r["events"] if r else []):
        d = json.loads(e["detail"]) if e.get("detail") else {}
        ed = d.get("event_detail")
        if ed == "finish":
            accepted = bool(d.get("accepted")); merged = bool(d.get("merged"))
            pushed = d.get("pushed")
        elif ed in ("architect_not_resolved", "architect_surfaced_decision", "pm_escalated"):
            hb = d.get("human_brief") or ({"question": d.get("question")} if d.get("question") else {})
            if hb.get("question"):
                brief = hb                      # the last one wins: the PM's phrasing supersedes
        elif ed == "run_cost":
            cost = d.get("paid_usd")
        elif ed == "agent_no_envelope":
            transient = True                       # an agent produced no output — infra/model failure
            cooldown = max(cooldown, float(d.get("retry_after_s") or 0.0))
        elif ed == "technical_unresolved":
            # Findings the architect could not close. Work, not a ruling — see Lane._unresolved.
            rework = list(d.get("findings") or [])
        elif ed == "run_aborted_unavailable":
            transient = True                       # the lane stopped itself: undecidable, not a ruling
            cooldown = max(cooldown, float(d.get("retry_after_s") or 0.0))
    blocking = [] if accepted else _blocking(run_id)
    return accepted, merged, run_id, blocking, transient, cooldown, cost, brief, pushed, rework


def _target_of(item):
    return item["id"] if item["kind"] == "foundation" else item.get("journey", item["id"])


# --- provider preflight: never spend a build the judge family cannot certify ----------------------
class JudgeGate:
    """Holds dispatch while the cross-family reviewer's provider is down.

    A build is only worth what an independent reviewer can sign off, so dispatching into a
    rate-limited judge family produces nothing usable — on 2026-08-18 that was a full 40-minute
    Anthropic build per attempt against an exhausted OpenAI account, five attempts deep. The probe
    is a single trivial completion: a fraction of a cent when the family is up, free when it is
    down (a rate limit errors immediately). Healthy verdicts are cached for `ttl` so a busy queue
    is not probing every poll; an unhealthy one parks dispatch for the cooldown the provider itself
    advertised, floored at 5 minutes and capped at an hour."""
    def __init__(self, probe=None, ttl=JUDGE_PROBE_TTL, clock=time.monotonic, log=print):
        self._probe = probe or (lambda: omp.probe("reviewer"))
        self._ttl, self._clock, self._log = ttl, clock, log
        self._ok_until = self._hold_until = 0.0

    def ready(self) -> bool:
        now = self._clock()
        if now < self._hold_until:
            return False
        if now < self._ok_until:
            return True
        try:
            ok, wait, err = self._probe()
        except Exception as ex:                        # a check that breaks must not stop the factory
            self._log(f"  ⋯ judge preflight failed ({ex}) — dispatching anyway", flush=True)
            self._ok_until = now + self._ttl
            return True
        if ok:
            self._ok_until = now + self._ttl
            return True
        self._hold_until = now + min(max(wait, 300), 3600)
        self._log(f"  ⋯ hold      dispatch — no judge route available (subscription and fallback both "
                  f"down), nothing built now could be judged ({err[:70]}); "
                  f"re-probing in {int(self._hold_until - now)}s", flush=True)
        return False


# --- decomposition gate (Rev4 §9): only parallelize streams with disjoint artifact bindings -------


def _bindings(item) -> frozenset:
    """The artifact bindings the control plane schedules on, NAMESPACED BY PROJECT.

    A journey's bindings are its canon `Touches` entities; a foundation (or an unresolvable journey)
    is treated as touching everything IN ITS OWN REPO, so it runs alone there. This is what lets the
    daemon run non-overlapping streams in parallel and serialize overlapping ones.

    The namespace is what makes the portfolio work. Bare entity names collide across repos — two
    products both having a `User` would have been serialized for no reason — and a bare `*` from any
    foundation conflicted with EVERY other item, so one project's foundation build would have
    blocked every other project in the portfolio. Different repos share nothing by construction:
    separate worktrees, separate git, separate canon."""
    repo = item.get("repo") or "?"
    everything = frozenset({f"{repo}:*"})
    if item.get("kind") == "foundation":
        return everything
    jid = item.get("journey") or item["id"]
    try:
        b = canon.CanonResolver(Path(repo_path(item["repo"]))).bindings(jid)
    except Exception:
        b = set()
    return frozenset(f"{repo}:{e}" for e in b) if b else everything


def _repos_of(binding: frozenset) -> set:
    return {x.split(":", 1)[0] for x in binding}


def _overlaps(a: frozenset, b: frozenset) -> bool:
    """Two streams conflict only within one repo: a per-repo wildcard, or a shared entity."""
    ra, rb = _repos_of(a), _repos_of(b)
    if not (ra & rb):
        return False                                   # different projects never contend
    if any(x.endswith(":*") and x.split(":", 1)[0] in rb for x in a):
        return True
    if any(x.endswith(":*") and x.split(":", 1)[0] in ra for x in b):
        return True
    return bool(a & b)


def run_daemon(max_parallel=3, poll_s=20, max_merge_retries=3):
    """The factory as a running system, not a pipeline. Loops forever: dispatch up to N ready items
    CONCURRENTLY (each isolated in its own worktree), reap finished ones, and NEVER stop on a
    decision — an escalation parks the item as `escalated` and the loop keeps churning everything
    else, re-picking work the instant a human unblocks it. Run under systemd for 24/7 operation."""
    nt = notifier_mod.get_notifier()
    inflight: dict[str, tuple] = {}                    # item_id -> (live subprocess, its bindings)
    merge_retries: dict[str, int] = {}
    infra_retries: dict[str, int] = {}                 # transient (model overloaded / no envelope) retries
    rework_retries: dict[str, int] = {}                # unclosed technical findings -> remediation retries
    judge = JudgeGate()                                # preflight: is the reviewer family up?
    migration_claims: dict[str, str] = {}              # item_id -> reserved migration number
    cycle_failures = 0                                 # consecutive cycles that raised
    delivery_warned: dict[str, str] = {}               # repo -> last delivery warning printed
    dispatch_blocked: set[str] = set()                 # repos whose work is not shipped
    retry_after: dict[str, float] = {}                 # item_id -> monotonic time before which not to redispatch
    conflicts = 0                                      # merge-conflict feedback (should stay near zero)
    # A prior daemon may have died mid-flight; systemd's cgroup kill takes its children too, so any
    # in_progress item is stale — reset it to ready so it re-runs cleanly.
    def _reset_stale(d):
        for it in d.get("items", []):
            if it.get("status") == "in_progress":
                it["status"] = "ready"
        return d

    statefile.update(BACKLOG, _reset_stale, header=BACKLOG_HEADER, default={"items": []})
    print(f"▶ daemon up — max_parallel={max_parallel}, poll={poll_s}s, continuous", flush=True)
    while True:
        # A cycle must never take the daemon down with it. Before 2026-08-21 any raise in here —
        # a malformed backlog, a git hiccup, an unreachable notifier — killed the process; systemd
        # restarted it, the cgroup kill reaped every in-flight lane, and hours of work vanished with
        # no record. A failing cycle is now logged and retried; only a persistently broken one stops.
        try:
            data = load(); items = data["items"]; by_id = {i["id"]: i for i in items}
            pending: dict[str, dict] = {}          # only what THIS cycle changed, written under lock
            # --- reap finished runs ------------------------------------------------
            for iid, (proc, _b) in list(inflight.items()):
                if proc.poll() is None:
                    continue
                del inflight[iid]
                migration_claims.pop(iid, None)             # claim released with the run
                item = by_id.get(iid)
                if not item:
                    continue
                accepted, merged, run_id, blocking, transient, cooldown, cost, brief, pushed, rework = _verdict(_target_of(item))
                item["paid_usd"] = cost; pending.setdefault(iid, {})["paid_usd"] = cost
                price = "" if cost is None else f"  (${cost:.2f} paid)"
                item["run_id"] = run_id; pending.setdefault(iid, {})["run_id"] = run_id
                if accepted and merged:
                    item["status"] = "accepted"; pending[iid] = {"status": "accepted"}
                    print(f"  ✓ accepted  {iid}{price}", flush=True)
                    if pushed is False:
                        # Merged locally but the remote never got it. The work is real but unshipped —
                        # say it every cycle until someone pushes, never let it pass as done.
                        item["status"] = "accepted_unpushed"; pending[iid] = {"status": "accepted_unpushed"}
                        print(f"  ⚠ NOT PUSHED {iid} — merged locally only; the remote does not have "
                              f"this work. Push {item.get('repo','?')} before trusting it shipped.",
                              flush=True)
                    nt.accepted(project=item.get("repo", "—"), item_id=iid, kind=item["kind"],
                                note=item.get("note", ""), run_id=run_id)
                elif (not accepted) and transient:           # infra failure (model overloaded / no envelope)
                    n = infra_retries.get(iid, 0) + 1; infra_retries[iid] = n
                    if n <= 5:
                        item["status"] = "ready"; pending[iid] = {"status": "ready"}   # NOT an escalation — retry, backed off
                        # A provider that advertises its own cooldown (a rate limit says "wait 30 min")
                        # outranks our schedule: retrying inside that window just burns the other family's
                        # tokens on a run that cannot finish. Cap at an hour so a bogus number can't park
                        # the queue indefinitely.
                        back = min(max(min(60 * n, 600), int(cooldown)), 3600)
                        retry_after[iid] = time.monotonic() + back
                        why = f"provider cooldown {int(cooldown)}s" if cooldown > min(60 * n, 600) else "transient infra failure"
                        print(f"  ↻ retry     {iid}  ({why}, attempt {n}, backoff {back}s)", flush=True)
                    else:
                        # NOT "escalated": there is nothing here for a human to rule, and marking it
                        # so parks every dependent until someone notices. infra_hold is dispatchable
                        # again as soon as a judge route is healthy (see ready()).
                        item["status"] = "infra_hold"; pending[iid] = {"status": "infra_hold"}
                        escalate(item, run_id,
                                 ["repeated transient infra failure (model overloaded / no envelope)"],
                                 operational=True)
                        nt.post(project=item.get("repo", "—"),
                                text=(f"⏸ {iid} on INFRASTRUCTURE HOLD after {n} provider failures. "
                                      f"No ruling needed — it reopens automatically when a judge route "
                                      f"is healthy. Run: {run_id}"),
                                facet="product")
                        print(f"  ⏸ infra-hold {iid}  (infra x{n} — reopens when providers recover)", flush=True)
                elif (not accepted) and rework:              # the architect could not CLOSE it
                    # Unfinished engineering, not a ruling. Sending a defect to the owner asks them
                    # to decide something no decision can settle; JRN-B1 did exactly that with five
                    # implementation findings on 2026-08-21. Re-dispatch as remediation, carrying
                    # the findings, and only escalate once rework itself keeps failing -- which
                    # means something structural (a canon gap, or a journey too large for one pass)
                    # and IS worth the owner's attention.
                    n = rework_retries.get(iid, 0) + 1; rework_retries[iid] = n
                    if n <= MAX_REWORK:
                        fpath = REWORK_DIR / f"{iid}-{n}.md"
                        REWORK_DIR.mkdir(parents=True, exist_ok=True)
                        fpath.write_text("# Findings the architect could not close\n\n"
                                         + "\n".join(f"- {f}" for f in rework) + "\n")
                        item["status"] = "ready"; pending[iid] = {"status": "ready"}
                        item["kind"] = "remediation"
                        item["findings"] = str(fpath.relative_to(FACTORY_ROOT))
                        item.setdefault("journey", _target_of(item))
                        pending[iid].update({"kind": "remediation",
                                             "findings": item["findings"],
                                             "journey": item["journey"]})
                        print(f"  ↻ rework    {iid}  ({len(rework)} unclosed finding(s), attempt {n}/{MAX_REWORK})",
                              flush=True)
                    else:
                        item["status"] = "escalated"; pending[iid] = {"status": "escalated"}
                        escalate(item, run_id,
                                 [f"rework failed {n}x — the factory cannot close these findings on its "
                                  f"own. This is likely a canon gap or a journey too large to build in "
                                  f"one pass, and needs decomposition or a ruling.", *rework])
                        nt.escalation(project=item.get("repo", "—"), item_id=iid, kind=item["kind"],
                                      note=item.get("note", ""), run_id=run_id, blocking=rework)
                        print(f"  ⚑ escalated {iid}  (rework x{n} — structural, needs you)", flush=True)
                elif accepted and not merged:                # lost the merge race on a moving base (I11)
                    conflicts += 1                           # decomposition-gate feedback signal
                    n = merge_retries.get(iid, 0) + 1; merge_retries[iid] = n
                    if n <= max_merge_retries:
                        item["status"] = "ready"; pending[iid] = {"status": "ready"}   # rebuild on the new base
                        print(f"  ↻ re-queue  {iid}  (merge race, attempt {n}/{max_merge_retries})", flush=True)
                    else:
                        item["status"] = "escalated"; pending[iid] = {"status": "escalated"}
                        escalate(item, run_id, ["repeated merge conflict — could not land on a moving base (I11)"])
                        nt.escalation(project=item.get("repo", "—"), item_id=iid, kind=item["kind"],
                                      note=item.get("note", ""), run_id=run_id,
                                      blocking=["repeated merge conflict (I11)"])
                        print(f"  ⚑ escalated {iid}  (merge race x{n})", flush=True)
                else:                                        # genuine escalation — park, notify, keep going
                    item["status"] = "escalated"; pending[iid] = {"status": "escalated"}
                    escalate(item, run_id, blocking, human_brief=brief)
                    nt.escalation(human_brief=brief, project=item.get("repo", "—"), item_id=iid, kind=item["kind"],
                                  note=item.get("note", ""), run_id=run_id, blocking=blocking)
                    print(f"  ⚑ escalated {iid}{price}  → awaiting your ruling (factory keeps running)", flush=True)
            # --- allocate + dispatch (Rev4 §9): WIP limit + decomposition gate ----
            # Only co-schedule streams whose artifact bindings are DISJOINT from everything already in
            # flight (and from each other this cycle). Overlapping work is serialized — it waits for the
            # conflicting run to finish — instead of racing to a merge conflict. This is what makes it
            # the designed `allocate` step rather than a naive parallel loop.
            held = [b for (_p, b) in inflight.values()]    # bindings currently in flight
                # DELIVERY GATE. One check per cycle per repo: refuse to start new work on a repo whose
            # accepted output has not reached its remote, or that is behind it. Shipping is part of
            # the job, not an afterthought.
            for _repo in {i.get("repo") for i in ready(items) if i.get("repo")}:
                ok_sync, why = delivery_state(_repo)
                if not ok_sync:
                    if delivery_warned.get(_repo) != why:
                        delivery_warned[_repo] = why
                        print(f"  ⚠ delivery   {why}", flush=True)
                    dispatch_blocked.add(_repo)
                else:
                    dispatch_blocked.discard(_repo)
                    delivery_warned.pop(_repo, None)

            dispatchable = [i for i in ready(items) if i["id"] not in inflight
                            and i.get("repo") not in dispatch_blocked
                            and retry_after.get(i["id"], 0) <= time.monotonic()]
            # Nothing is dispatched while the judge family is down — a build that cannot be reviewed is
            # spend with no possible outcome. Checked only when there is actually something to dispatch.
            for item in (dispatchable if (dispatchable and len(inflight) < max_parallel
                                          and judge.ready()) else []):
                if len(inflight) >= max_parallel:
                    break
                if item["id"] in inflight:
                    continue
                if retry_after.get(item["id"], 0) > time.monotonic():
                    continue                               # backing off a transient failure — not yet due
                b = _bindings(item)
                if any(_overlaps(b, h) for h in held):
                    print(f"  ⋯ hold      {item['id']}  (bindings {sorted(b)} overlap in-flight — serialized)", flush=True)
                    continue                               # decomposition gate: serialize, don't collide
                # FRONT-END CANON CHECK. Resolve the journey against the schema BEFORE spending a
                # run on it: JRN-B1 burned five hours and thirteen review cycles rediscovering that
                # its migration and RPCs did not exist. A gap found here is architect work, stated
                # up front, and it travels into the run so the first review already knows.
                brief_path = ""
                if item.get("kind") == "journey":
                    try:
                        rep = readiness.check(Path(repo_path(item["repo"])), _target_of(item))
                    except Exception:
                        rep = None
                    if rep is not None and not rep.ready:
                        bdir = FACTORY_ROOT / "runs" / "readiness"
                        bdir.mkdir(parents=True, exist_ok=True)
                        bp = bdir / f"{item['id']}.md"
                        bp.write_text(rep.brief())
                        brief_path = str(bp)
                        print(f"  \u26a0 readiness  {item['id']} starts with "
                              f"{len(rep.blocking())} known substrate gap(s) — handed to the run up "
                              f"front, not discovered by review", flush=True)
                        for gap in rep.blocking()[:3]:
                            print(f"       · {gap}", flush=True)
                item["status"] = "in_progress"; pending[item["id"]] = {"status": "in_progress"}
                claim = claim_migration_number(item, migration_claims.values())
                migration_claims[item["id"]] = claim
                # children stay in the daemon's cgroup, so systemd stop/restart reaps them (no orphans)
                proc = subprocess.Popen(_cmd(item), cwd=str(FACTORY_ROOT),
                                        env={**os.environ, "FACTORY_MIGRATION_CLAIM": claim,
                                         "FACTORY_READINESS_BRIEF": brief_path})
                inflight[item["id"]] = (proc, b)
                held.append(b)
                print(f"  ▶ dispatch  {item['id']}  ({item['kind']}) bindings={sorted(b)} "
                      f"[{len(inflight)}/{max_parallel} in flight, {conflicts} merge-conflicts so far]", flush=True)
            # Deltas only. A whole-file write here would erase any ruling approved in the observatory
            # during this cycle — which is exactly what it did before 2026-08-21.
            save_fields(pending)
        except Exception as ex:
            cycle_failures += 1
            print(f"  ✗ cycle failed ({type(ex).__name__}: {str(ex)[:120]}) "
                  f"[{cycle_failures}/{MAX_CYCLE_FAILURES}] — retrying", flush=True)
            if cycle_failures >= MAX_CYCLE_FAILURES:
                print("  ⚑ daemon stopping: the loop cannot complete a cycle", flush=True)
                raise
        else:
            cycle_failures = 0
        time.sleep(poll_s)


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
    ap.add_argument("--daemon", action="store_true",
                    help="run forever: dispatch N journeys concurrently, park escalations, never stop")
    ap.add_argument("--max-parallel", type=int, default=3, help="max concurrent journeys in --daemon")
    ap.add_argument("--poll", type=int, default=20, help="daemon poll interval seconds")
    a = ap.parse_args(argv)
    if not BACKLOG.exists():
        print(f"no backlog at {BACKLOG}"); sys.exit(1)
    if a.daemon:
        run_daemon(max_parallel=a.max_parallel, poll_s=a.poll)
    else:
        run_orchestrator(dry_run=a.dry_run, once=a.once)


if __name__ == "__main__":
    main(sys.argv[1:])
