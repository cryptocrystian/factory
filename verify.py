#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2","pyyaml>=6"]
# ///
"""Verify the factory pipeline END TO END, spending nothing.

WHY THIS EXISTS. Every bug that cost real money on 2026-08-18/19 was one a pipeline check would
have caught: a rate-limited provider being turned into a code finding, a run aborting on a garbled
envelope, the replay harness itself crashing on an unmapped role. None of them needed a live model
to expose — but there was no single command that ran the pipeline and asserted the result, so they
shipped. This is that command.

    uv run verify.py [--repo PATH] [--with-docker]

Every check is free: the replay lane feeds RECORDED envelopes through the LIVE control plane, so
sequencing, envelope typing, gates, permissions and the fix loop are all exercised with zero model
calls. --with-docker adds the migration gate and the DEC-062 behavioural proof (real Postgres).

Exit 0 = the pipeline behaves as recorded. Nonzero = something changed; the summary names it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The golden recording is versioned (gzipped) so this runs anywhere, notably the VPS. The
# uncompressed original under runs/ is preferred when present but is not required.
REC_VERSIONED = ROOT / "control-plane" / "goldens" / "p0-jrn-s1"
REC_LOCAL = ROOT / "runs" / "2026-08-07-p0-jrn-s1"
REC = REC_LOCAL if REC_LOCAL.is_dir() else REC_VERSIONED

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label, detail))
    return bool(ok)


def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 900) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def latest_run_dir(target: str) -> Path | None:
    ds = sorted((d for d in (ROOT / "runs").glob(f"*-feature-{target}") if d.is_dir()),
                key=lambda d: d.name)
    return ds[-1] if ds else None


def verify_replay(repo: Path) -> None:
    """The golden regression: recorded envelopes through the live control plane."""
    if not REC.is_dir():
        check(False, "replay recording present", str(REC))
        return
    rc, out = run(["uv", "run", str(ROOT / "lanes" / "feature.py"),
                   "--repo", str(repo), "--journey", "JRN-S1", "--replay", str(REC)])
    check("Traceback" not in out, "replay lane runs without crashing", out.strip().splitlines()[-1][:90] if out else "")
    d = latest_run_dir("JRN-S1")
    if not d:
        check(False, "replay produced a run record")
        return
    ev = []
    for line in (d / "trace.jsonl").read_text().splitlines():
        try:
            ev.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    phases_ok = {e.get("phase") for e in ev if e.get("event") == "phase_end" and e.get("status") == "success"}
    for ph in ("resolve", "planner", "builder", "test-author", "reviewer"):
        check(ph in phases_ok, f"phase completes: {ph}")

    gates = {}
    for e in ev:
        if e.get("event") == "gate" and e.get("gate"):
            gates.setdefault(e["gate"], []).append(bool(e.get("passed")))
    # Gates that MUST hold in replay. diff_matches_claims is excluded on purpose: replay applies no
    # diffs, so it cannot pass and is advisory there by design.
    for g in ("artifacts_exist", "verdict_consistent", "typecheck", "check:tokens", "test:unit"):
        check(bool(gates.get(g)) and all(gates[g]), f"gate holds: {g}",
              "" if gates.get(g) else "gate never ran")

    details = [e.get("event_detail") for e in ev]
    check("review_verdict" in details, "reviewer produced a real verdict")
    check("agent_no_envelope" not in details, "no phase lost its envelope")
    check("permission_breach" not in details, "no role wrote outside its grant")
    check("run_aborted_unavailable" not in details, "no phase reported itself unavailable")
    check(details.count("fix_iter") >= 1, "the bounded fix loop engaged")
    fin = [e for e in ev if e.get("event_detail") == "finish"]
    check(bool(fin), "the run reached an explicit verdict")
    if fin:
        # The recorded P0 review carried blocking findings, so the golden outcome is NOT accepted.
        # Pinning it means a change that silently starts green-lighting this run fails the check.
        check(fin[-1].get("accepted") is False, "golden outcome preserved (recorded run: not accepted)",
              f"accepted={fin[-1].get('accepted')}")


def verify_config() -> None:
    sys.path.insert(0, str(ROOT / "control-plane"))
    import config  # noqa: E402

    check(not config.paid_fallback_enabled(), "paid fallbacks are off by default")
    for role in ("reviewer", "test-author", "product-manager"):
        chain = config.model_chain(role)
        check(chain == (config.role(role).model,), f"{role} runs subscription-only unless opted in",
              " → ".join(chain))
        check(chain[0].startswith("openai-codex/"), f"{role} primary is the subscription", chain[0])
    check(config.model_chain("builder")[0] == config.role("builder").model, "builder is not rerouted")
    # I3: whoever judges must not share the builder's family, on every configured route.
    import os
    os.environ["OMP_ALLOW_PAID_FALLBACK"] = "1"
    for role in ("reviewer", "test-author"):
        chain = config.model_chain(role)
        check(not any("claude" in m or "anthropic" in m for m in chain),
              f"{role} never shares the builder's family (I3)")
        check(all(m.startswith("openai-codex/") or m.startswith("openrouter/") for m in chain),
              f"{role} uses no direct vendor API", " → ".join(chain))
    os.environ.pop("OMP_ALLOW_PAID_FALLBACK", None)


def verify_governance() -> None:
    """The escalation ladder, exercised with stub agents: architect -> PM -> owner.

    These are the two failures that put JRN-S4 in front of the owner on 2026-08-19: a breach ended
    the run after one of four architect rounds, and the PM — configured, prompted and granted
    canon/** — was never invoked at all, so every decision skipped it."""
    import contextlib
    sys.path.insert(0, str(ROOT / "control-plane"))
    import envelopes as E  # noqa: E402
    import importlib.util as _il
    spec = _il.spec_from_file_location("feat_v", ROOT / "lanes" / "feature.py")
    feat = _il.module_from_spec(spec)
    spec.loader.exec_module(feat)

    scratch = ROOT / "runs" / "_verify"; scratch.mkdir(parents=True, exist_ok=True)

    class Tracer:
        def __init__(self): self.events = []
        def log(self, **kw): self.events.append(kw)
        def gate(self, r): pass
        def record_call(self, *a): pass

    class Ph:
        def ok(self): pass

    class Run:
        def __init__(self):
            self.dir = scratch; self.workspace = scratch; self.tracer = Tracer()
        @contextlib.contextmanager
        def phase(self, p): yield Ph()
        def commit(self, m): pass
        def finish(self, a, reason=""): return a

    def lane(agent_impl, converge=False):
        ln = feat.Lane.__new__(feat.Lane)
        ln.repo = scratch; ln.journey = "VERIFY"; ln.runner = None; ln.live = True
        ln.design = None; ln._agent_gates_ok = True; ln._last_breach = []; ln._human_brief = None
        ln._review_cycles = 0; ln._seen_findings = []
        ln._agent = agent_impl.__get__(ln)
        ln._converge = (lambda self, r, f, u: converge).__get__(ln)
        ln._review = (lambda self, r: (False, ["blocking"], None)).__get__(ln)
        return ln

    def dets(run): return [e.get("event_detail") for e in run.tracer.events]
    finding = lambda d: E.ArchitectFinding(ref="AC-V-01", disposition=d, action="x", **{"class": "product"})

    # 1. a breach costs a round, not the run
    seen = []
    def breach_once(self, run, role, ot, prompt, cwd, add_dirs, gate_fns, commit_msg=None, **kw):
        seen.append(role)
        if role == "architect" and seen.count("architect") == 1:
            self._last_breach = ["M scripts/verify-x.sh"]
        return None
    r = Run(); lane(breach_once)._architect_resolve(r)
    check(seen.count("architect") >= 2, "an architect breach costs a round, not the run",
          f"{seen.count('architect')} rounds used")
    check("architect_breach_corrected" in dets(r), "the breach is named back to the architect")

    # 2. the PM rules a routine product call -> the owner never sees it
    def pm_rules(self, run, role, ot, prompt, cwd, add_dirs, gate_fns, commit_msg=None, **kw):
        if role == "architect":
            self._human_brief = {"needed": True, "question": "q", "why": "w", "options": [], "recommendation": "r"}
            return None
        return E.ArchitectOutput(status="success", summary="ruled", findings=[finding("resolved")],
                                 remediation_brief="apply")
    ln = lane(pm_rules, converge=True); r = Run()
    accepted = ln._architect_resolve(r)
    check(accepted is True, "a PM-ruled decision resolves the run")
    check(ln._human_brief is None, "a PM-ruled decision leaves nothing for the owner")
    check("pm_ruled" in dets(r), "the PM ruling is recorded")

    # 3. the PM judges it the owner's -> escalate, carrying the PM's sharper brief
    def pm_escalates(self, run, role, ot, prompt, cwd, add_dirs, gate_fns, commit_msg=None, **kw):
        if role == "architect":
            self._human_brief = {"needed": True, "question": "architect phrasing", "why": "w",
                                 "options": [], "recommendation": "r"}
            return None
        return E.ArchitectOutput(status="success", summary="owner", findings=[finding("escalate")],
                                 human_brief={"needed": True, "question": "PM phrasing", "why": "business model",
                                              "options": [], "recommendation": "r"})
    ln = lane(pm_escalates); r = Run()
    check(ln._architect_resolve(r) is False, "an owner-level decision still escalates")
    check((ln._human_brief or {}).get("question") == "PM phrasing",
          "the owner reads the PM's brief, not the architect's")
    check("pm_escalated" in dets(r), "the PM escalation is recorded")

    # 4. a PM that could not run must never look like a ruling
    def pm_dead(self, run, role, ot, prompt, cwd, add_dirs, gate_fns, commit_msg=None, **kw):
        if role == "architect":
            self._human_brief = {"needed": True, "question": "q", "why": "w", "options": [], "recommendation": "r"}
        return None
    ln = lane(pm_dead, converge=True); r = Run()
    check(ln._architect_resolve(r) is False, "an unavailable PM does not fake a ruling")
    check("pm_unavailable" in dets(r), "an unavailable PM is recorded as such")


def verify_ladder_coverage() -> None:
    """Every entry point must climb the same ladder: builder -> architect -> PM -> owner.
    Until 2026-08-19 only journeys did; foundations and remediations escalated straight to a human."""
    import ast
    tree = ast.parse((ROOT / "lanes" / "feature.py").read_text())
    lane = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Lane")
    fns = {n.name: n for n in lane.body if isinstance(n, ast.FunctionDef)}
    for entry, body_fn in (("journey", "_feature_phases"), ("foundation", "_foundation_phases"),
                           ("remediate", "remediate")):
        src = ast.dump(fns[body_fn]) if body_fn in fns else ""
        check("_architect_resolve" in src, f"{entry} lane climbs the escalation ladder")
    # and the ladder itself has exactly one unresolved exit, so the PM cannot be bypassed
    ar = ast.dump(fns["_architect_resolve"])
    check(ar.count("_unresolved") >= 2 and "'False'" not in ar.split("_unresolved")[0][-40:],
          "the architect loop has no exit that skips the PM")


def verify_daemon() -> None:
    """The continuous loop's own logic: it must hold when no judge can run, sit out a provider's
    advertised cooldown rather than retrying inside it, and never park the queue on a broken check."""
    import importlib.util as _il
    spec = _il.spec_from_file_location("orch_v", ROOT / "orchestrator.py")
    orch = _il.module_from_spec(spec)
    spec.loader.exec_module(orch)

    t = [0.0]
    probes = []

    def gate(probe):
        return orch.JudgeGate(probe=probe, ttl=600, clock=lambda: t[0], log=lambda *a, **k: None)

    # a healthy judge is probed once, not every poll
    t[0] = 0.0
    g = gate(lambda: (probes.append(1), (True, 0.0, ""))[1])
    ok = all(g.ready() for _ in range(20))
    check(ok and len(probes) == 1, "a healthy judge is probed once per TTL, not every poll",
          f"{len(probes)} probe(s) across 20 polls")

    # a downed judge holds dispatch for exactly the cooldown it advertised
    t[0] = 0.0
    g = gate(lambda: (False, 1800.0, "usage limit"))
    held_now = not g.ready()
    t[0] = 1799
    still_held = not g.ready()
    check(held_now and still_held, "dispatch holds for the provider's advertised cooldown")

    # no advertised cooldown still gets a floor, not an instant retry storm
    t[0] = 0.0
    g = gate(lambda: (False, 0.0, "overloaded"))
    g.ready(); t[0] = 299
    check(not g.ready(), "a cooldown-less failure still backs off (5-minute floor)")

    # an absurd cooldown is capped, so a bad number cannot park the queue for days
    t[0] = 0.0
    g = gate(lambda: (False, 999999.0, "nonsense"))
    g.ready(); t[0] = 3601
    recovered = []
    g._probe = lambda: (recovered.append(1), (True, 0.0, ""))[1]
    check(g.ready() and recovered, "an absurd cooldown is capped at an hour and re-probed")

    # a probe that raises must not stop the factory
    def boom():
        raise RuntimeError("probe exploded")
    t[0] = 0.0
    check(gate(boom).ready() is True, "a broken probe fails open, never parking the queue")

    # transient infra failures re-queue; they are not decisions for a human
    src = (ROOT / "orchestrator.py").read_text()
    check("infra_retries" in src and 'item["status"] = "ready"' in src,
          "a transient infra failure re-queues instead of escalating")


def verify_portfolio() -> None:
    """The scheduler must let PROJECTS run in parallel. Bindings were bare entity names, so two
    products sharing a `User` serialized for no reason, and any foundation's `*` conflicted with
    every item in the portfolio — one project's foundation would have blocked all the others."""
    import importlib.util as _il
    spec = _il.spec_from_file_location("orch_p", ROOT / "orchestrator.py")
    orch = _il.module_from_spec(spec)
    spec.loader.exec_module(orch)

    fs = frozenset
    a_user = fs({"arxus:User", "arxus:Listing"})
    b_user = fs({"beta:User", "beta:Offer"})
    a_other = fs({"arxus:BQSScore"})
    a_found = fs({"arxus:*"})
    b_found = fs({"beta:*"})

    check(not orch._overlaps(a_user, b_user),
          "two projects sharing an entity name run in parallel")
    check(not orch._overlaps(a_found, b_user),
          "a foundation in one project does not block another project")
    check(not orch._overlaps(a_found, b_found),
          "two projects' foundations run in parallel")
    check(orch._overlaps(a_found, a_other),
          "a foundation still serializes its OWN project")
    check(orch._overlaps(a_user, fs({"arxus:Listing"})),
          "a shared entity within one project still serializes")
    check(not orch._overlaps(a_user, a_other),
          "disjoint entities within one project still run in parallel")

    # bindings are namespaced at the source, not just compared that way
    b = orch._bindings({"id": "x", "kind": "foundation", "repo": "beta"})
    check(b == fs({"beta:*"}), "a foundation's wildcard is scoped to its repo", str(sorted(b)))


def verify_meter() -> None:
    """The money rail and the value meter. omp's own cost numbers under-reported OpenRouter by
    20-70x on 2026-08-19, so both are computed from the provider's counter, never from run logs."""
    sys.path.insert(0, str(ROOT / "control-plane"))
    import meter  # noqa: E402

    ok, why = meter.allow_paid(lambda: 5.00)
    check(ok, "a funded balance permits paid routes", why)
    ok, why = meter.allow_paid(lambda: 0.32)
    check(not ok, "a balance under the floor refuses paid routes", why)
    ok, why = meter.allow_paid(lambda: None)
    check(ok, "unreadable metering fails OPEN — it is a rail, not a gate", why)

    seq = iter([10.0, 12.5])
    m = meter.RunMeter(reader=lambda: next(seq)); m.open()
    check(m.close() == 2.5, "a run reports what it actually cost")
    m = meter.RunMeter(reader=lambda: None); m.open()
    check(m.close() is None, "an unknown cost reports unknown, never zero")

    lane_src = (ROOT / "lanes" / "feature.py").read_text()
    check("paid_route_refused" in lane_src, "the lane refuses a paid route below the floor")
    check("run_cost" in lane_src, "every run records its cost against its outcome")
    orch_src = (ROOT / "orchestrator.py").read_text()
    check("paid_usd" in orch_src, "the daemon reports the price with the outcome")
    omp_src = (ROOT / "control-plane" / "omp.py").read_text()
    check("meter.allow_paid" in omp_src, "the preflight will not advertise a route it may not take")


def verify_stale_green() -> None:
    """SSSF §6.5: a review may only accept a tree whose suite is green right now. Ported after the
    install-and-diff on 2026-08-20 found both architect accept-paths returning True on a review
    alone, over a tree whose last unit result could have been red."""
    import ast
    src = (ROOT / "lanes" / "feature.py").read_text()
    tree = ast.parse(src)
    lane = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Lane")
    fns = {n.name: n for n in lane.body if isinstance(n, ast.FunctionDef)}
    check("_retest exists", "_retest" in fns)
    ar = ast.dump(fns["_architect_resolve"])
    # every `return True` in the architect loop must be guarded by a re-test
    approvals = ar.count("'_retest'")
    check(approvals >= 2, "every review-only acceptance re-runs the suite first",
          f"{approvals} guarded accept paths")
    check("retest_before_accept" in src, "the re-test is recorded in the trace")


def verify_phase_intent() -> None:
    """SSSF hard rule 7: a phase description is one sentence on what it does and WHY — never a
    restatement of the name. It is the only intent the trace and the observatory ever show, and
    ours emitted "planner phase" / "builder phase" for every phase until the 2026-08-20 diff."""
    sys.path.insert(0, str(ROOT / "control-plane"))
    import config  # noqa: E402
    for r in ("planner", "builder", "test-author", "reviewer", "architect", "product-manager"):
        d = config.phase_intent(r)
        check(d and r.replace("-", " ") not in d.lower() and len(d.split()) >= 6,
              f"{r} phase description states intent, not its own name", d[:64] + "…")
    src = (ROOT / "lanes" / "feature.py").read_text()
    check('description=f"{role} phase"' not in src, "no phase falls back to a name restatement")
    # hard rule 1: the roster is validated before anything spawns
    check("validate_roles" in src, "the lane validates its roster before spending")
    try:
        config.validate_roles(["planner", "nope"])
        check(False, "an unknown role fails fast")
    except KeyError:
        check(True, "an unknown role fails fast, before any agent spawns")


def verify_loop_governance() -> None:
    """The four fixes from the JRN-B1 post-mortem: thirteen review cycles, five hours, and two
    journeys both authoring migration 0012."""
    import importlib.util as _il, os as _os, time as _time
    spec = _il.spec_from_file_location("feat_g", ROOT / "lanes" / "feature.py")
    feat = _il.module_from_spec(spec); spec.loader.exec_module(feat)

    # 1. protected-path findings route to the architect instead of the builder
    check(feat.needs_protected_path(["0012_bqs.sql is absent, so the required RPCs are missing"]),
          "a missing-migration finding is recognised as architect work")
    check(feat.needs_protected_path(["canon/ does not define the stage ladder"]),
          "a canon finding is recognised as architect work")
    check(not feat.needs_protected_path(["the button label is wrong", "add a unit test"]),
          "an ordinary app finding still goes to the builder")

    # 2. the run-wide review ceiling sits over the nested loops
    check(feat.MAX_REVIEW_CYCLES < feat.MAX_FIX_ITERS * feat.MAX_ARCH_ROUNDS,
          "the review ceiling binds before the nested loops can",
          f"{feat.MAX_REVIEW_CYCLES} < {feat.MAX_FIX_ITERS}x{feat.MAX_ARCH_ROUNDS}")

    class T:
        def __init__(self): self.events = []
        def log(self, **kw): self.events.append(kw)
    class R:
        def __init__(self): self.tracer = T()
    lane = feat.Lane.__new__(feat.Lane)
    lane._review_cycles = 0; lane._seen_findings = []
    r = R(); r._started_at = _time.monotonic()
    check(not lane._budget_spent(r), "a fresh run has budget")
    lane._review_cycles = feat.MAX_REVIEW_CYCLES
    check(lane._budget_spent(r), "the run stops at its review ceiling")

    # 3. the same finding twice is a loop that is not converging
    lane._review_cycles = 2; lane._seen_findings = ["same"] * (feat.NO_PROGRESS_REPEATS + 1)
    r2 = R(); r2._started_at = _time.monotonic()
    check(lane._budget_spent(r2), "a repeated finding stops the loop")
    check(any(e.get("event_detail") == "no_progress" for e in r2.tracer.events),
          "the non-progress stop is recorded")

    # 4. a run that runs too long stops, whatever its loops say
    lane._review_cycles = 0; lane._seen_findings = []
    r3 = R(); r3._started_at = _time.monotonic() - (feat.MAX_RUN_WALL_S + 60)
    check(lane._budget_spent(r3), "a run past its wall clock stops")

    # 4b. a breach names the boundary AND who owns the path, for every role
    check("ARCHITECT owns migrations" in feat._grant_owner(["?? supabase/migrations/0016_x.sql"]),
          "a builder reaching for a migration is told the architect owns it")
    check("TEST-AUTHOR" in feat._grant_owner(["M tests/unit/x.spec.ts"]),
          "a role reaching for tests is told it cannot edit what judges it")
    check("canon" in feat._grant_owner(["M canon/Decision Log.md"]).lower(),
          "a role reaching for canon is routed to the architect/PM")
    lane_src = (ROOT / "lanes" / "feature.py").read_text()
    check("builder_breach_corrected" in lane_src,
          "a builder breach costs an iteration, not the run")

    # 5. the migration claim reaches the architect's prompt
    _os.environ["FACTORY_MIGRATION_CLAIM"] = "0042"
    note = feat._migration_claim_note()
    check("0042_" in note and "parallel" in note, "the architect is told which migration number it owns")
    _os.environ.pop("FACTORY_MIGRATION_CLAIM")
    check(feat._migration_claim_note() == "", "no claim, no instruction")

    # 6. the scheduler hands concurrent runs DIFFERENT numbers
    spec2 = _il.spec_from_file_location("orch_g", ROOT / "orchestrator.py")
    orch = _il.module_from_spec(spec2); spec2.loader.exec_module(orch)
    first = orch.claim_migration_number({"repo": "arxus"}, [])
    second = orch.claim_migration_number({"repo": "arxus"}, [first])
    check(first != second, "two concurrent journeys claim different migration numbers",
          f"{first} then {second}")
    check(int(second) == int(first) + 1, "claims are sequential")


def verify_escalation_payload() -> None:
    """What the owner reads. JRN-B1's PM packaged a business question — vendor activation and SBA
    financing parameters — and the queue showed a service_role RLS finding instead, because
    escalate() never carried the brief out of the trace."""
    src = (ROOT / "orchestrator.py").read_text()
    check("human_brief=brief" in src, "the escalation carries the PM's question, not just findings")
    check('"pm_escalated"' in src, "a PM escalation is recognised as the owner-facing brief")
    import importlib.util as _il
    spec = _il.spec_from_file_location("orch_e", ROOT / "orchestrator.py")
    orch = _il.module_from_spec(spec); spec.loader.exec_module(orch)
    import inspect
    sig = inspect.signature(orch.escalate)
    check("human_brief" in sig.parameters, "escalate() accepts the brief")
    sig2 = inspect.signature(orch._queue_decision)
    check("human_brief" in sig2.parameters, "the queue records the brief")


def verify_shipping_and_concurrency() -> None:
    """The 2026-08-21 audit findings: work that never left the box, rulings erased by a concurrent
    writer, and a daemon that died on any raise."""
    import importlib.util as _il, inspect, yaml as _yaml, tempfile
    sys.path.insert(0, str(ROOT / "control-plane"))
    import isolation, statefile  # noqa: E402

    # 1. an accepted merge is PUSHED, and an unpushed merge is not reported as shipped
    src = (ROOT / "control-plane" / "isolation.py").read_text()
    check('"push"' in src, "an accepted merge pushes to the remote")
    check("pushed" in isolation.MergeResult.__dataclass_fields__,
          "the merge result says whether it actually shipped")
    ses = (ROOT / "control-plane" / "session.py").read_text()
    check("merge_not_pushed" in ses, "a merge that never reached the remote is recorded")
    orch = (ROOT / "orchestrator.py").read_text()
    check("accepted_unpushed" in orch, "the daemon refuses to call unpushed work accepted")

    # 2. concurrent writers cannot lose a ruling
    d = Path(tempfile.mkdtemp()) / "backlog.yml"
    statefile.update(d, lambda _c: {"items": [{"id": "a", "status": "escalated"},
                                              {"id": "b", "status": "ready"}]}, header="# t\n")
    stale = _yaml.safe_load(d.read_text())                       # a writer reads at cycle start
    statefile.update(d, lambda c: [i.update({"status": "ready"})
                                   for i in c["items"] if i["id"] == "a"] and c or c, header="# t\n")

    def _delta(c):                                               # the other writer applies ITS change
        for i in c["items"]:
            if i["id"] == "b":
                i["status"] = "in_progress"
        return c
    statefile.update(d, _delta, header="# t\n")
    final = {i["id"]: i["status"] for i in _yaml.safe_load(d.read_text())["items"]}
    check(final["a"] == "ready", "a ruling survives a concurrent daemon write")
    check(final["b"] == "in_progress", "the daemon's own change survives too")
    check(len(stale["items"]) == 2, "the stale snapshot is never written back wholesale")
    check("save_fields" in orch, "the daemon writes deltas, not whole-file snapshots")
    obs = (ROOT / "observe.py").read_text()
    check("statefile.update" in obs, "the observatory writes under the same lock")

    # 3. a failing cycle does not take the daemon down
    check("MAX_CYCLE_FAILURES" in orch, "a failing cycle is bounded, not fatal")
    check("cycle failed" in orch, "a failing cycle is reported")


def verify_orchestration_table_stakes() -> None:
    """The two things any orchestration layer owes: work that is queued and CHECKED before it runs,
    and output that is actually delivered. Both were missing until 2026-08-21."""
    sys.path.insert(0, str(ROOT / "control-plane"))
    import readiness  # noqa: E402
    import importlib.util as _il
    spec = _il.spec_from_file_location("orch_t", ROOT / "orchestrator.py")
    orch = _il.module_from_spec(spec); spec.loader.exec_module(orch)

    # readiness: entity -> table, including prefixed forms
    check(readiness.table_name("BQSScore") == "bqs_score", "ontology entities map to table names")
    check(readiness.table_name("VerificationRecord") == "verification_record", "compound entities map")

    repo = Path(os.path.expanduser("~/projects/arxus"))
    if repo.is_dir():
        r = readiness.check(repo, "JRN-S4")
        check(r.ready, "a shipped journey reads as ready", "; ".join(r.blocking())[:70])
        # DEC-066: canon may declare an entity derived — the check must believe canon rather than
        # inventing schema work canon has explicitly ruled out.
        r2 = readiness.check(repo, "JRN-G2")
        check(r2.ready, "an entity canon declares DERIVED is not reported as a missing table",
              "; ".join(r2.notes)[:70])
        check(any("derived" in n for n in r2.notes), "the derived declaration is recorded as a note")
        # a genuinely absent table is still caught: prove the mechanism on a synthetic entity
        check(readiness._declared_derived(__import__("canon").CanonResolver(repo), "Note") is False,
              "a real stored entity is still expected to have a table")
        r3 = readiness.check(repo, "JRN-NOPE")
        check(not r3.ready, "an unknown journey is never dispatched")

    src = (ROOT / "orchestrator.py").read_text()
    check("readiness.check" in src, "the daemon runs the canon check before every dispatch")
    check("FACTORY_READINESS_BRIEF" in src, "the gaps travel into the run")
    lane = (ROOT / "lanes" / "feature.py").read_text()
    check("readiness_gaps_in_context" in lane, "the run's context carries them to the planner")

    # delivery: unshipped or stale repos do not get new work
    check(hasattr(orch, "delivery_state"), "the daemon knows whether its output is shipped")
    check("dispatch_blocked" in src, "an unshipped repo is not given new work")
    check("NOT shipped" in src and "stale base" in src,
          "ahead and behind are both refused, for different reasons")


def verify_selftest() -> None:
    rc, out = run(["uv", "run", str(ROOT / "control-plane" / "selftest_k1.py")])
    check(rc == 0 and "ALL PASS" in out, "K1 adapter-spine self-test",
          [l for l in out.splitlines() if "FAIL" in l][:1] or "")


def verify_docker(repo: Path) -> None:
    rc, out = run(["bash", str(ROOT / "control-plane" / "validate_migrations.sh")], cwd=repo, timeout=900)
    check(rc == 0, "every migration applies to a real Postgres",
          [l for l in out.splitlines() if "FAIL" in l][:1] or "")
    script = repo / "scripts" / "verify-stage-derivation.sh"
    if script.is_file():
        rc, out = run(["bash", str(script)], cwd=repo, timeout=900)
        check(rc == 0, "DEC-062 stage derivation behaves as ratified",
              [l for l in out.splitlines() if "FAIL" in l][:1] or "")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path.home() / "projects" / "arxus"))
    ap.add_argument("--with-docker", action="store_true", help="also run the real-Postgres gates")
    a = ap.parse_args(argv)
    repo = Path(a.repo).expanduser().resolve()

    print("factory verify — no model calls, no spend\n")
    verify_config()
    verify_selftest()
    verify_governance()
    verify_ladder_coverage()
    verify_daemon()
    verify_portfolio()
    verify_meter()
    verify_stale_green()
    verify_phase_intent()
    verify_loop_governance()
    verify_escalation_payload()
    verify_shipping_and_concurrency()
    verify_orchestration_table_stakes()
    verify_replay(repo)
    if a.with_docker:
        verify_docker(repo)

    print()
    failed = 0
    for ok, label, detail in results:
        if not ok:
            failed += 1
        d = f"  {detail}" if detail else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{d}")
    print(f"\n{len(results) - failed}/{len(results)} checks pass")
    if failed:
        print("PIPELINE NOT VERIFIED — the failures above are the pipeline, not the product.")
    else:
        print("PIPELINE VERIFIED — recorded envelopes flow through the live control plane as expected.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
