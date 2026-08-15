#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "pyyaml>=6"]
# ///
"""The feature lane — the factory running a journey end to end, in code (I1).

  resolve -> plan -> [build -> L0 -> fix]* -> test-author -> unit -> review -> finish(accepted)

Every phase: a bounded OMP session (or a replayed recording), a real gate audit, post-hoc
write-enforcement, and a PHASE-BOUNDARY COMMIT. accepted = (L0 green ∧ unit green ∧ review
approved) — one explicit decision (I5). Two runners: Live (drives OMP, resumes on timeout) and
Replay (feeds recorded P0 envelopes through the same control plane — the golden regression)."""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

CP = Path(__file__).resolve().parent.parent / "control-plane"
sys.path.insert(0, str(CP))

MAX_FIX_ITERS = 3          # bounded builder fix loop (I9): converge or escalate
MAX_ARCH_ROUNDS = 2        # bounded architect authority loop: resolve protected-path findings or escalate

import config, omp, gates, permissions as perm
import envelopes as E
from canon import CanonResolver
from session import Run


# ----------------------------------------------------------------------------- runners
class LiveRunner:
    """Drive OMP; on a mid-phase timeout, resume the same session — bounded by BOTH a retry count and a
    per-phase wall-clock cap (I9), so a phase that can't converge escalates instead of churning."""
    def run(self, call: E.AgentCall, run: Run, workspace: Path):
        budget = config.Budget()
        start = time.monotonic()
        res = omp.run(call, run.dir, workspace, run.tracer)
        tries = 0
        while (res.timed_out or not res.ok) and tries < budget.max_retries_per_phase:
            if not res.session_id:
                break
            elapsed = time.monotonic() - start
            if elapsed > budget.max_wall_s:                 # per-phase wall cap: give up, let the lane escalate
                run.tracer.log(event_detail="phase_wall_exceeded", role=call.role,
                               elapsed_s=int(elapsed), max_wall_s=budget.max_wall_s)
                break
            call = replace_call(call, resume_session=res.session_id)
            res = omp.run(call, run.dir, workspace, run.tracer)
            tries += 1
        return res


class ReplayRunner:
    """Feed recorded envelopes from a prior run through the live control plane."""
    MAP = {"planner": "plan-phase.jsonl", "builder": "build-fix2.jsonl",
           "test-author": "test-fix.jsonl", "reviewer": "review-phase.jsonl"}

    def __init__(self, rec_dir: Path):
        self.rec = Path(rec_dir)

    def run(self, call: E.AgentCall, run: Run, workspace: Path):
        lines = (self.rec / self.MAP[call.role]).read_text().splitlines()
        sid, final, cost, n = omp._parse_stream(lines)
        env = E.ENVELOPE_TYPES[call.output_type].model_validate_json(omp._json_slice(final))
        if call.role == "planner" and (self.rec / "plan.md").exists():
            (run.dir / "plan.md").write_text((self.rec / "plan.md").read_text())   # recreate the plan artifact
        run.tracer.record_call(call.role, n, cost, False)
        return omp.AgentResult(ok=True, envelope=env, session_id=sid, cost_usd=cost,
                               timed_out=False, events=n, raw_final=final)


def replace_call(call: E.AgentCall, **kw) -> E.AgentCall:
    return call.model_copy(update=kw)


# ----------------------------------------------------------------------------- the lane
class Lane:
    def __init__(self, repo: Path, journey: str, runner, live: bool):
        self.repo = Path(repo).resolve()
        self.journey = journey
        self.runner = runner
        self.live = live
        self.design = config.design_policy(self.repo)   # None unless the repo opts into the anti-slop gate
        self._agent_gates_ok = True   # agent-phase gates enforce on live runs (reset per pass/fix-iter)

    def run(self) -> bool:
        run = Run(self.repo, lane="feature", target=self.journey)
        self._agent_gates_ok = True
        self._human_brief = None                 # set by the architect when it surfaces a decision
        run.tracer.log(event_detail="lane_start", journey=self.journey, mode="live" if self.live else "replay")

        # -- resolve (I7: canon gap halts) ------------------------------------
        with run.phase(E.PhaseParams(name="resolve", kind="code", owner="canon",
                       description="Resolve governing canon for the journey by binding")) as ph:
            bundle = CanonResolver(self.repo).resolve(self.journey)
            if bundle.gaps and any("not found" in g for g in bundle.gaps):
                run.tracer.log(event_detail="canon_gap", gaps=bundle.gaps)
                return run.finish(False, reason=f"canon gap: {bundle.gaps}")
            (run.dir / "context.md").write_text(bundle.markdown())
            ph.ok()

        # -- plan (writes plan.md into the run dir; not committed to the repo) --
        plan = self._agent(run, "planner", "plan",
                           prompt=self._planner_prompt(run),
                           cwd=run.dir, add_dirs=[self.repo],
                           gate_fns=[gates.artifacts_exist])
        if plan is None:
            return run.finish(False, reason="planning failed")

        # -- build (writes source into the repo; L0 gate + bounded fix) --------
        l0_ok = self._build(run, plan)

        # -- test-author (different family; writes tests only) + unit gate -----
        unit_ok = self._test(run)

        # -- review (different family; read-only) + verdict gate ---------------
        approved, findings, review = self._review(run)
        accepted = l0_ok and unit_ok[0] and approved and self._agent_gates_ok
        if not accepted:
            accepted = self._converge(run, findings, unit_ok[1])
        # The builder can't write protected paths (migrations, canon). When it can't converge, the
        # architect — the authority over those paths — takes over: it resolves technical findings
        # against canon and escalates only genuine business/product decisions. This is what closes
        # the governance loop so a human is not the trigger.
        if not accepted:
            accepted = self._architect_resolve(run)
        return run.finish(accepted, reason="" if accepted else self._escalation_reason())

    def remediate(self, findings: str) -> bool:
        """Close open review findings on an already-built journey, via the bounded fix loop."""
        run = Run(self.repo, lane="remediate", target=self.journey)
        run.tracer.log(event_detail="remediate_start", journey=self.journey)
        (run.dir / "context.md").write_text(CanonResolver(self.repo).resolve(self.journey).markdown())
        accepted = self._converge(run, [findings], "")
        return run.finish(accepted, reason="" if accepted else "did not converge")

    def foundation(self, brief: str) -> bool:
        """Build a foundation (auth, infra…) from a brief instead of a canon journey — same machinery.
        The brief IS the context/spec; the planner turns it into a plan, then build→verify→review→converge."""
        run = Run(self.repo, lane="foundation", target=self.journey)
        self._agent_gates_ok = True
        run.tracer.log(event_detail="foundation_start", target=self.journey)
        (run.dir / "context.md").write_text(brief)
        plan = self._agent(run, "planner", "plan",
                           prompt=("Read context.md — a foundation brief (a cross-cutting build, not a "
                                   "single user journey). Produce plan.md and your envelope per your "
                                   "system instructions."),
                           cwd=run.dir, add_dirs=[self.repo], gate_fns=[gates.artifacts_exist])
        if plan is None:
            return run.finish(False, reason="planning failed")
        l0_ok = self._build(run, plan)
        unit_ok = self._test(run)
        approved, findings, _ = self._review(run)
        accepted = l0_ok and unit_ok[0] and approved and self._agent_gates_ok
        if not accepted:
            accepted = self._converge(run, findings, unit_ok[1])
        return run.finish(accepted, reason="" if accepted else "did not converge")

    # -- phase helpers ---------------------------------------------------------
    def _agent(self, run, role, output_type, prompt, cwd, add_dirs, gate_fns, commit_msg=None):
        r = config.role(role)
        with run.phase(E.PhaseParams(name=role, kind="agent", owner=role,
                       description=f"{role} phase", writes=config.WRITE_GRANTS.get(role))) as ph:
            call = E.AgentCall(role=role, output_type=output_type, prompt=prompt,
                               add_dirs=[str(d) for d in add_dirs])
            res = self.runner.run(call, run, cwd)
            if not res.ok:
                run.tracer.log(event_detail="agent_no_envelope", role=role, error=res.error)
                return None
            # write-enforcement against the isolated worktree (planner writes to run dir, not the
            # workspace, so enforce only for roles that operate in the worktree)
            if cwd == run.workspace:
                pr = perm.enforce(run, role)
                if not pr.ok:
                    run.tracer.log(event_detail="phase_aborted_breach", role=role)
                    return None
            for g in gate_fns:
                rep = g(res.envelope, run)
                run.tracer.gate(rep)
                if not rep.passed:
                    run.tracer.log(event_detail="gate_failed", role=role, gate=rep.gate)
                    # Live runs ENFORCE: a failed agent-phase gate blocks acceptance and routes to
                    # the bounded fix loop. Replay feeds recorded envelopes without re-applying diffs,
                    # so diff/artifact gates can't be satisfied there — advisory in replay by design.
                    if self.live:
                        self._agent_gates_ok = False
            if commit_msg:
                run.commit(getattr(res.envelope, "commit_message", None) or commit_msg)
            ph.ok()
            return res.envelope

    def _build(self, run, plan) -> bool:
        env = self._agent(run, "builder", "build",
                         prompt=self._builder_prompt(run, plan),
                         cwd=run.workspace, add_dirs=[run.dir],
                         gate_fns=[gates.diff_matches_claims])
        if env is None:
            return False
        # L0 gates against the repo (design:slop appended only when the repo opts in — I8)
        l0 = True
        for g in self._l0_gates():
            rep = g(None, run); run.tracer.gate(rep); l0 = l0 and rep.passed
        run.commit(getattr(env, "commit_message", None) or f"build: {self.journey}")
        return l0

    def _l0_gates(self):
        """The deterministic L0 command gates, plus the anti-slop design gate for opted-in repos."""
        gs = [gates.cmd_gate("check:tokens", "npm run gen:tokens >/dev/null 2>&1; npm run check:tokens"),
              gates.cmd_gate("typecheck", "npm run typecheck")]
        if self.design:
            gs.append(gates.impeccable_gate(self.design.impeccable_version, self.design.detect_paths))
        return gs

    def _design_selfcheck(self) -> str:
        """A builder-prompt clause telling opted-in repos to clear the detector before returning."""
        if not self.design:
            return ""
        return (f" This repo enforces the anti-slop design gate: run "
                f"`npx impeccable detect {self.design.detect_paths}` and clear every finding, "
                f"honoring DESIGN.md and .impeccable/config.json.")

    def _test(self, run, prompt=None) -> tuple[bool, str]:
        self._agent(run, "test-author", "test",
                    prompt=prompt or self._test_prompt(run),
                    cwd=run.workspace, add_dirs=[run.dir], gate_fns=[])
        rep = gates.cmd_gate("test:unit", "npm run test:unit")(None, run)
        run.tracer.gate(rep)
        run.commit(f"test: {self.journey} acceptance tests")
        return rep.passed, rep.evidence

    def _review(self, run):
        review = self._agent(run, "reviewer", "review",
                           prompt=self._reviewer_prompt(run),
                           cwd=run.workspace, add_dirs=[run.dir],
                           gate_fns=[gates.verdict_consistent])
        approved = bool(getattr(review, "approved", False)) if review else False
        blocking = getattr(review, "blocking", []) if review else ["no review envelope"]
        run.tracer.log(event_detail="review_verdict", approved=approved, blocking=blocking)
        return approved, blocking, review

    def _converge(self, run, findings, unit_evidence) -> bool:
        """Bounded fix loop: re-route findings + failing-gate output to the builder until accepted."""
        for i in range(1, MAX_FIX_ITERS + 1):
            run.tracer.log(event_detail="fix_iter", i=i, findings=findings)
            self._agent_gates_ok = True        # this iteration's builder/reviewer gates must pass
            fixmd = "# Findings to close\n\n" + "\n".join(f"- {f}" for f in findings)
            if unit_evidence:
                fixmd += f"\n\n## Failing unit output\n```\n{unit_evidence[-1500:]}\n```"
            (run.dir / f"findings-{i}.md").write_text(fixmd)
            fix = self._agent(run, "builder", "build",
                             prompt=(f"Close the findings in findings-{i}.md (added run dir), SOURCE only. "
                                     "Read context.md for governing canon. Self-verify "
                                     "gen:tokens/check:tokens/typecheck." + self._design_selfcheck()
                                     + " Return your envelope."),
                             cwd=run.workspace, add_dirs=[run.dir], gate_fns=[gates.diff_matches_claims])
            l0 = True
            for g in self._l0_gates():
                rep = g(None, run); run.tracer.gate(rep); l0 = l0 and rep.passed
            run.commit(getattr(fix, "commit_message", None) or f"fix({self.journey}): iter {i}")
            unit_ok, unit_evidence = self._test(
                run, prompt=("Reconcile/extend the acceptance tests to the current source and the findings "
                             f"in findings-{i}.md. Run npm run test:unit; leave a real defect failing and "
                             "name it. Tests only. Return your envelope."))
            approved, findings, _ = self._review(run)
            if l0 and unit_ok and approved and self._agent_gates_ok:
                run.tracer.log(event_detail="converged", iter=i)
                return True
        run.tracer.log(event_detail="not_converged", iters=MAX_FIX_ITERS)
        return False

    def _architect_resolve(self, run) -> bool:
        """The architect authority loop — closes the governance hole. Invoked when the builder can't
        converge, which is almost always because the fix lives in a protected path the builder may
        not write (a migration, canon). The architect triages the reviewer's blocking findings,
        resolves the technical ones against canon (authoring the migration/decision — verified by a
        deterministic Postgres gate, not self-attestation), hands app-logic fixes back to the builder,
        and escalates ONLY genuine business/product decisions. Bounded. True iff the build is accepted.

        The write-grant (permissions.py) structurally confines the architect to migrations + canon: it
        cannot touch tests or app source, so it can never green a build by weakening the tests. Canon
        it can write — the never-weaken-canon rule is held by its charter, the different-family tests
        it cannot edit, and the audit trail here."""
        approved, findings, _ = self._review(run)          # current blocking findings post-builder-loop
        if approved:
            return True
        for i in range(1, MAX_ARCH_ROUNDS + 1):
            (run.dir / f"arch-findings-{i}.md").write_text(
                "# Blocking findings the builder could not close\n\n" + "\n".join(f"- {f}" for f in findings))
            arch = self._agent(run, "architect", "architect",
                prompt=(f"The builder's fix loop could not close the findings in arch-findings-{i}.md "
                        "(added run dir) — they require a protected path only you may write (a database "
                        "migration or a canonical decision). Read context.md for governing canon. Triage "
                        "each finding; resolve the technical ones by authoring the migration/decision that "
                        "makes the build satisfy canon, and validate any migration against real Postgres "
                        "(docker). NEVER weaken canon, tests, or invariants to pass. Follow the launch-gate "
                        "doctrine (DEC-052/055/056): when the open question is a provider/dataset/roster, "
                        "BUILD the mechanism behind the seam with a build seed so the journey is acceptable "
                        "now, and record the real roster as a launch-gate sign-off in human_brief — do NOT "
                        "leave the build blocked on it. Return your envelope."),
                cwd=run.workspace, add_dirs=[run.dir], gate_fns=[])
            if arch is None:
                return False
            # Record any surfaced decision as a PENDING SIGN-OFF — do not treat it as a blocker here.
            # Whether it truly blocks is decided by the reviewer below: if the build (with the seam +
            # build seed the architect installed) passes review, the decision is a non-blocking
            # launch-gate sign-off surfaced to the owner; if review still fails, the run escalates.
            hb = getattr(arch, "human_brief", {}) or {}
            if hb.get("needed"):
                self._human_brief = hb
                (run.dir / "human_brief.md").write_text(
                    f"# Decision for the owner\n\n**Q:** {hb.get('question','')}\n\n"
                    f"**Why:** {hb.get('why','')}\n\n**Options:** {hb.get('options',[])}\n\n"
                    f"**Recommendation:** {hb.get('recommendation','')}\n")
                run.tracer.log(event_detail="architect_surfaced_decision", human_brief=hb)
            # Verify architect-authored migrations by CODE (agent proposes, code disposes).
            if (run.workspace / "supabase" / "migrations").exists():
                mg = gates.migration_gate()(None, run)
                run.tracer.gate(mg)
                if not mg.passed:
                    findings = [f"your migration did not apply cleanly — fix it: {mg.evidence[-600:]}"]
                    continue                       # let the architect repair its own migration next round
            # Commit the architect's resolution (migrations/canon), THEN apply the app-logic remediation
            # via the builder's bounded loop. The reviewer inside _converge is the arbiter of acceptance.
            run.commit(getattr(arch, "commit_message", None) or f"architect({self.journey}): round {i}")
            brief = getattr(arch, "remediation_brief", "") or \
                    "Apply the architect's resolution; make the build satisfy canon. Source only."
            if self._converge(run, [brief], ""):
                # Accepted. Any surfaced decision is a NON-BLOCKING launch-gate sign-off (fail-closed),
                # recorded for the owner — it did not block the green.
                run.tracer.log(event_detail="architect_resolved", round=i,
                               pending_signoff=bool(self._human_brief))
                return True
            approved, findings, _ = self._review(run)       # fresh findings for the next architect round
            if approved:
                run.tracer.log(event_detail="architect_resolved", round=i,
                               pending_signoff=bool(self._human_brief))
                return True
        # Rounds exhausted without acceptance: the residue genuinely blocks — escalate (with the
        # architect's packaged decision if it surfaced one, else the technical residue).
        run.tracer.log(event_detail="architect_not_resolved", rounds=MAX_ARCH_ROUNDS,
                       human_brief=self._human_brief or {})
        return False

    def _escalation_reason(self) -> str:
        """What the run reports on escalation. A surfaced decision is a packaged business/product
        question (→ the human/PM); otherwise it's an unresolved technical residue."""
        hb = getattr(self, "_human_brief", None)
        if hb and hb.get("needed"):
            q = hb.get("question", "")
            return f"decision needed: {q}" if q else "decision needed (see architect human_brief)"
        return "did not converge (architect could not resolve technically)"

    # -- prompts (live only; replay ignores them) ------------------------------
    def _planner_prompt(self, run):
        return ("Read context.md in your working directory and produce plan.md + your envelope, "
                "exactly per your system instructions.")
    def _builder_prompt(self, run, plan):
        return ("Read plan.md in the added run dir and context.md; implement the journey as source "
                "only (no tests). Self-verify gen:tokens/check:tokens/typecheck."
                + self._design_selfcheck() + " Return your envelope.")
    def _test_prompt(self, run):
        return ("Read the acceptance criteria in context.md and the built source; write the acceptance "
                "tests under tests/ (unit runnable, integration/e2e authored). Run npm run test:unit. "
                "Return your envelope.")
    def _reviewer_prompt(self, run):
        return ("Independently review the build against context.md's acceptance criteria and decisions. "
                "You may run the gates. Return your verdict envelope.")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--journey", required=True)
    ap.add_argument("--replay", default="", help="recorded run dir to replay envelopes from")
    ap.add_argument("--remediate", default="", help="path to a findings file: run the fix->test->re-review loop")
    ap.add_argument("--foundation", default="", help="path to a brief file: build a foundation (not a canon journey)")
    a = ap.parse_args(argv)
    live = not a.replay
    runner = ReplayRunner(Path(a.replay)) if a.replay else LiveRunner()
    lane = Lane(Path(a.repo), a.journey, runner, live)
    if a.remediate:
        accepted = lane.remediate(Path(a.remediate).read_text())
    elif a.foundation:
        accepted = lane.foundation(Path(a.foundation).read_text())
    else:
        accepted = lane.run()
    print(f"\nfeature lane -> accepted={accepted}")
    sys.exit(0 if accepted else 2)


if __name__ == "__main__":
    main(sys.argv[1:])
