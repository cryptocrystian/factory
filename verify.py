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
        ln._agent = agent_impl.__get__(ln)
        ln._converge = (lambda self, r, f, u: converge).__get__(ln)
        ln._review = (lambda self, r: (False, ["blocking"], None)).__get__(ln)
        return ln

    def dets(run): return [e.get("event_detail") for e in run.tracer.events]
    finding = lambda d: E.ArchitectFinding(ref="AC-V-01", disposition=d, action="x", **{"class": "product"})

    # 1. a breach costs a round, not the run
    seen = []
    def breach_once(self, run, role, ot, prompt, cwd, add_dirs, gate_fns, commit_msg=None):
        seen.append(role)
        if role == "architect" and seen.count("architect") == 1:
            self._last_breach = ["M scripts/verify-x.sh"]
        return None
    r = Run(); lane(breach_once)._architect_resolve(r)
    check(seen.count("architect") >= 2, "an architect breach costs a round, not the run",
          f"{seen.count('architect')} rounds used")
    check("architect_breach_corrected" in dets(r), "the breach is named back to the architect")

    # 2. the PM rules a routine product call -> the owner never sees it
    def pm_rules(self, run, role, ot, prompt, cwd, add_dirs, gate_fns, commit_msg=None):
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
    def pm_escalates(self, run, role, ot, prompt, cwd, add_dirs, gate_fns, commit_msg=None):
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
    def pm_dead(self, run, role, ot, prompt, cwd, add_dirs, gate_fns, commit_msg=None):
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
