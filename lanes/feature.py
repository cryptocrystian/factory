#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
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
from dataclasses import dataclass, replace
from pathlib import Path

CP = Path(__file__).resolve().parent.parent / "control-plane"
sys.path.insert(0, str(CP))

import config, omp, gates, permissions as perm
import envelopes as E
from canon import CanonResolver
from session import Run


# ----------------------------------------------------------------------------- runners
class LiveRunner:
    """Drive OMP; on a mid-build timeout, resume the same session (bounded)."""
    def run(self, call: E.AgentCall, run: Run, workspace: Path):
        res = omp.run(call, run.dir, workspace, run.tracer)
        tries = 0
        while (res.timed_out or not res.ok) and tries < config.Budget().max_retries_per_phase + 2:
            if not res.session_id:
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

    def run(self) -> bool:
        run = Run(self.repo, lane="feature", target=self.journey)
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
        review = self._agent(run, "reviewer", "review",
                            prompt=self._reviewer_prompt(run),
                            cwd=self.repo, add_dirs=[run.dir],
                            gate_fns=[gates.verdict_consistent])
        approved = bool(getattr(review, "approved", False)) if review else False

        accepted = l0_ok and unit_ok and approved
        blocking = getattr(review, "blocking", []) if review else ["no review"]
        run.tracer.log(event_detail="verdict", l0=l0_ok, unit=unit_ok, approved=approved,
                       blocking=blocking)
        return run.finish(accepted, reason="" if accepted else f"L0={l0_ok} unit={unit_ok} approved={approved} blocking={blocking}")

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
            # write-enforcement against the repo (planner writes to run dir, so enforce only repo roles)
            if cwd == self.repo:
                pr = perm.enforce(run, role)
                if not pr.ok:
                    run.tracer.log(event_detail="phase_aborted_breach", role=role)
                    return None
            for g in gate_fns:
                rep = g(res.envelope, run)
                run.tracer.gate(rep)
                if not rep.passed:
                    run.tracer.log(event_detail="gate_failed", role=role, gate=rep.gate)
                    # (bounded correction loop would resume here; recorded here as a finding)
            if commit_msg:
                run.commit(getattr(res.envelope, "commit_message", None) or commit_msg)
            ph.ok()
            return res.envelope

    def _build(self, run, plan) -> bool:
        env = self._agent(run, "builder", "build",
                         prompt=self._builder_prompt(run, plan),
                         cwd=self.repo, add_dirs=[run.dir],
                         gate_fns=[gates.diff_matches_claims])
        if env is None:
            return False
        # L0 gates against the repo
        l0 = True
        for g in [gates.cmd_gate("check:tokens", "npm run gen:tokens >/dev/null 2>&1; npm run check:tokens"),
                  gates.cmd_gate("typecheck", "npm run typecheck")]:
            rep = g(None, run); run.tracer.gate(rep); l0 = l0 and rep.passed
        run.commit(getattr(env, "commit_message", None) or f"build: {self.journey}")
        return l0

    def _test(self, run) -> bool:
        env = self._agent(run, "test-author", "test",
                         prompt=self._test_prompt(run),
                         cwd=self.repo, add_dirs=[run.dir],
                         gate_fns=[])
        rep = gates.cmd_gate("test:unit", "npm run test:unit")(None, run)
        run.tracer.gate(rep)
        run.commit(f"test: {self.journey} acceptance tests")
        return rep.passed

    # -- prompts (live only; replay ignores them) ------------------------------
    def _planner_prompt(self, run):
        return ("Read context.md in your working directory and produce plan.md + your envelope, "
                "exactly per your system instructions.")
    def _builder_prompt(self, run, plan):
        return ("Read plan.md in the added run dir and context.md; implement the journey as source "
                "only (no tests). Self-verify gen:tokens/check:tokens/typecheck. Return your envelope.")
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
    a = ap.parse_args(argv)
    live = not a.replay
    runner = ReplayRunner(Path(a.replay)) if a.replay else LiveRunner()
    accepted = Lane(Path(a.repo), a.journey, runner, live).run()
    print(f"\nfeature lane -> accepted={accepted}")
    sys.exit(0 if accepted else 2)


if __name__ == "__main__":
    main(sys.argv[1:])
