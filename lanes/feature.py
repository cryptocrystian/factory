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

# Every role this lane can call. Validated before a run spawns anything (hard rule 1).
LANE_ROLES = ("planner", "builder", "test-author", "reviewer", "architect", "product-manager")

MAX_FIX_ITERS = 3          # bounded builder fix loop (I9): converge or escalate
MAX_ARCH_ROUNDS = 4        # bounded architect authority loop: enough rounds to close a lockdown cascade

import config, omp, gates, meter, permissions as perm
import envelopes as E
from canon import CanonResolver
from session import Run


class ReplayGap(Exception):
    """The recording has no stream for a role the lane reached. Replay is a regression over a
    RECORDED path; a role that never ran when the recording was made cannot be replayed."""


class PhaseUnavailable(Exception):
    """A phase produced no output for a reason no amount of building can fix, so the run must STOP
    rather than fabricate work. Two cases:

      infra      — the provider itself failed (rate limit, overload). Nothing downstream is real.
      no_verdict — the reviewer emitted no envelope. Acceptance is undecidable, and feeding the
                   builder a synthetic "no review envelope" finding sends it to fix nothing: that
                   burned three builder iterations per journey on 2026-08-18 while the cross-family
                   reviewer sat rate-limited.

    Either way the run is re-queued by the daemon (it logs `agent_no_envelope`), not escalated to a
    human — there is no ruling to make."""
    def __init__(self, kind: str, role: str, error: str, retry_after_s: float = 0.0):
        super().__init__(f"{role}: {error}")
        self.kind, self.role, self.error, self.retry_after_s = kind, role, error, retry_after_s


# ----------------------------------------------------------------------------- runners
class LiveRunner:
    """Drive OMP; on a mid-phase timeout, resume the same session — bounded by BOTH a retry count and a
    per-phase wall-clock cap (I9), so a phase that can't converge escalates instead of churning.

    When the PROVIDER itself is down (rate limit, outage) no amount of resuming helps, so the phase
    is re-attempted on the role's fallback models instead — same role, same system prompt, same
    tools, a different route to the same judgment. The subscription is always position one, so the
    paid route is reached only when the free one has actually failed."""
    def run(self, call: E.AgentCall, run: Run, workspace: Path):
        chain = config.model_chain(call.role)
        res = self._attempt(call, run, workspace, model=None)
        for fallback in chain[1:]:
            if not res.infra_failed:
                break
            if fallback.startswith("openrouter/"):
                allowed, why = meter.allow_paid()
                if not allowed:
                    # Below the floor. Stopping here is the point: the run aborts and re-queues,
                    # which costs time, where continuing would drain the account and still not land.
                    run.tracer.log(event_detail="paid_route_refused", role=call.role,
                                   model=fallback, reason=why)
                    break
            run.tracer.log(event_detail="provider_fallback", role=call.role,
                           from_model=chain[0], to_model=fallback, error=res.error)
            res = self._attempt(replace_call(call, resume_session=None), run, workspace, model=fallback)
            if res.ok:
                run.tracer.log(event_detail="provider_fallback_ok", role=call.role, model=fallback)
        return res

    def _attempt(self, call: E.AgentCall, run: Run, workspace: Path, model: str | None):
        budget = config.Budget()
        start = time.monotonic()
        res = omp.run(call, run.dir, workspace, run.tracer, model=model)
        tries = 0
        while (res.timed_out or not res.ok) and tries < budget.max_retries_per_phase:
            if not res.session_id or res.infra_failed:
                break                                      # resuming into a downed provider is pointless
            elapsed = time.monotonic() - start
            if elapsed > budget.max_wall_s:                # per-phase wall cap: give up, let the lane escalate
                run.tracer.log(event_detail="phase_wall_exceeded", role=call.role,
                               elapsed_s=int(elapsed), max_wall_s=budget.max_wall_s)
                break
            call = replace_call(call, resume_session=res.session_id)
            res = omp.run(call, run.dir, workspace, run.tracer, model=model)
            tries += 1
        return res


class ReplayRunner:
    """Feed recorded envelopes from a prior run through the live control plane."""
    MAP = {"planner": "plan-phase.jsonl", "builder": "build-fix2.jsonl",
           "test-author": "test-fix.jsonl", "reviewer": "review-phase.jsonl"}

    def __init__(self, rec_dir: Path):
        self.rec = Path(rec_dir)

    def _recorded(self, name: str) -> list[str]:
        """A recorded stream, plain or gzipped. The golden set ships compressed (10.5MB -> 1.5MB)
        so it can live in version control and the regression runs on every machine, including the
        VPS where the factory actually runs and where runs/ is gitignored."""
        plain = self.rec / name
        if plain.is_file():
            return plain.read_text().splitlines()
        gz = self.rec / f"{name}.gz"
        if gz.is_file():
            import gzip
            return gzip.decompress(gz.read_bytes()).decode().splitlines()
        raise ReplayGap(f"no recorded stream {name!r} in {self.rec}")

    def run(self, call: E.AgentCall, run: Run, workspace: Path):
        if call.role not in self.MAP:
            raise ReplayGap(f"no recorded stream for role {call.role!r} in {self.rec.name} — "
                            f"replay covers {sorted(self.MAP)}")
        lines = self._recorded(self.MAP[call.role])
        sid, final, cost, n, _perr, _wait = omp._parse_stream(lines)
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
        self._last_breach: list[str] = []   # paths a role tried to write outside its grant (rolled back)
        self._human_brief = None

    def run(self) -> bool:
        run = Run(self.repo, lane="feature", target=self.journey)
        self._agent_gates_ok = True
        self._human_brief = None                 # set by the architect when it surfaces a decision
        config.validate_roles(LANE_ROLES)                 # hard rule 1: fail before anything spawns
        run.tracer.log(event_detail="lane_start", journey=self.journey, mode="live" if self.live else "replay")
        try:
            return self._feature_phases(run)
        except PhaseUnavailable as e:
            return self._abort(run, e)

    def _feature_phases(self, run) -> bool:
        run.meter = meter.RunMeter().open()
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
        # Live only. The architect is the authority path over protected files; replay feeds recorded
        # envelopes and has no architect stream to feed it, so entering the loop here would crash the
        # regression rather than test it — which is exactly what it did until 2026-08-19, leaving the
        # only zero-cost end-to-end check un-runnable from the day the architect was added.
        if not accepted and self.live:
            accepted = self._architect_resolve(run)
        self._record_cost(run, accepted)
        return run.finish(accepted, reason="" if accepted else self._escalation_reason())

    def _record_cost(self, run, accepted: bool) -> None:
        """What this run cost in real money, against what it produced. Cost alone is not a metric —
        cost per LANDED journey is."""
        m = getattr(run, "meter", None)
        if m is None:
            return
        spend = m.close()
        run.tracer.log(event_detail="run_cost", paid_usd=spend, accepted=bool(accepted),
                       journey=self.journey)

    def remediate(self, findings: str) -> bool:
        """Close open review findings on an already-built journey, via the bounded fix loop."""
        run = Run(self.repo, lane="remediate", target=self.journey)
        self._agent_gates_ok = True
        self._human_brief = None
        run.tracer.log(event_detail="remediate_start", journey=self.journey)
        (run.dir / "context.md").write_text(CanonResolver(self.repo).resolve(self.journey).markdown())
        try:
            accepted = self._converge(run, [findings], "")
            # The ladder is the same for every kind of work: builder, then the architect over
            # protected paths, then the PM over product calls, and only then a human. Without this
            # a remediation escalated to the owner having consulted neither.
            if not accepted:
                accepted = self._architect_resolve(run)
        except PhaseUnavailable as e:
            return self._abort(run, e)
        return run.finish(accepted, reason="" if accepted else self._escalation_reason())

    def foundation(self, brief: str) -> bool:
        """Build a foundation (auth, infra…) from a brief instead of a canon journey — same machinery.
        The brief IS the context/spec; the planner turns it into a plan, then build→verify→review→converge."""
        run = Run(self.repo, lane="foundation", target=self.journey)
        self._agent_gates_ok = True
        run.tracer.log(event_detail="foundation_start", target=self.journey)
        (run.dir / "context.md").write_text(brief)
        try:
            return self._foundation_phases(run)
        except PhaseUnavailable as e:
            return self._abort(run, e)

    def _foundation_phases(self, run) -> bool:
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
        if not accepted:
            accepted = self._architect_resolve(run)      # same ladder as a journey (see remediate)
        return run.finish(accepted, reason="" if accepted else self._escalation_reason())

    def _abort(self, run, e: PhaseUnavailable) -> bool:
        """End the run on an undecidable phase. NOT an escalation: there is no ruling for a human to
        make about a rate-limited provider or a reviewer that never answered. The trace carries the
        provider's requested cooldown so the daemon can wait exactly that long before re-queueing."""
        run.tracer.log(event_detail="run_aborted_unavailable", kind=e.kind, role=e.role,
                       error=e.error, retry_after_s=e.retry_after_s)
        return run.finish(False, reason=f"{e.role} unavailable ({e.kind}): {e.error}")

    # -- phase helpers ---------------------------------------------------------
    def _agent(self, run, role, output_type, prompt, cwd, add_dirs, gate_fns, commit_msg=None,
               intent=None):
        r = config.role(role)
        with run.phase(E.PhaseParams(name=role, kind="agent", owner=role,
                       description=intent or config.phase_intent(role),
                       writes=config.WRITE_GRANTS.get(role))) as ph:
            call = E.AgentCall(role=role, output_type=output_type, prompt=prompt,
                               add_dirs=[str(d) for d in add_dirs])
            res = self.runner.run(call, run, cwd)
            if not res.ok:
                retry_after = getattr(res, "retry_after_s", 0.0)
                run.tracer.log(event_detail="agent_no_envelope", role=role, error=res.error,
                               provider_error=getattr(res, "provider_error", ""),
                               retry_after_s=retry_after)
                # A provider outage makes every later phase meaningless; a missing REVIEW envelope
                # makes acceptance undecidable. Both stop the run here (the daemon re-queues it)
                # instead of walking the builder through phantom findings.
                if getattr(res, "infra_failed", False):
                    raise PhaseUnavailable("infra", role, res.error, retry_after)
                if role == "reviewer":
                    raise PhaseUnavailable("no_verdict", role, res.error, retry_after)
                return None
            # write-enforcement against the isolated worktree (planner writes to run dir, not the
            # workspace, so enforce only for roles that operate in the worktree)
            if cwd == run.workspace:
                pr = perm.enforce(run, role)
                if not pr.ok:
                    run.tracer.log(event_detail="phase_aborted_breach", role=role)
                    # The write is already rolled back. Remember WHAT was attempted so the caller can
                    # hand the agent a corrective brief: a role reaching outside its grant is a
                    # recoverable mistake, and ending the run over it wastes every round it had left.
                    self._last_breach = list(pr.breaches)
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
        # _agent raises PhaseUnavailable when the reviewer emits no envelope, so a verdict here is
        # always a real one — no synthetic "no review envelope" finding ever reaches the builder.
        approved = bool(getattr(review, "approved", False))
        blocking = list(getattr(review, "blocking", []) or [])
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
                             intent=f"Close review round {i}'s findings in source, without weakening canon",
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
        if approved and self._retest(run):                 # never accept on a stale green (§6.5)
            return True
        for i in range(1, MAX_ARCH_ROUNDS + 1):
            (run.dir / f"arch-findings-{i}.md").write_text(
                "# Blocking findings the builder could not close\n\n" + "\n".join(f"- {f}" for f in findings))
            arch = self._agent(run, "architect", "architect",
                intent=f"Round {i}: author the migration or canon decision the builder may not write",
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
                breach, self._last_breach = self._last_breach, []
                if breach:
                    # It wrote somewhere it may not. That is a misunderstanding of its authority, not
                    # an unresolvable finding — name the grant and let it spend another round. On
                    # 2026-08-19 this ended JRN-S4 after ONE of four rounds, and escalated to the
                    # owner three findings that were the architect's own job to close.
                    grants = ", ".join(config.WRITE_GRANTS.get("architect") or [])
                    run.tracer.log(event_detail="architect_breach_corrected", round=i, breach=breach)
                    findings = [
                        f"Your write to {', '.join(breach)} was ROLLED BACK: it is outside your grant. "
                        f"You may write ONLY {grants}. Verification scripts, tests and app source belong "
                        "to other roles — you cannot edit what judges your work. Resolve the findings "
                        "below by authoring a migration or a canonical decision instead.",
                        *findings,
                    ]
                    continue
                return self._unresolved(run)
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
            if approved and self._retest(run):              # never accept on a stale green (§6.5)
                run.tracer.log(event_detail="architect_resolved", round=i,
                               pending_signoff=bool(self._human_brief))
                return True
        # Rounds exhausted. If the architect surfaced a DECISION, the owner is not the next stop —
        # the product manager is. It is a different family from the architect and it owns product
        # calls, so a question that is merely "canon doesn't say yet" gets ruled here and the build
        # continues. Only what the PM itself judges owner-level (pricing, legal, business model,
        # risk posture) reaches a human. Without this the PM was configured, prompted, granted
        # canon/** — and never once invoked, so every decision went straight to the owner.
        return self._unresolved(run)

    def _unresolved(self, run) -> bool:
        """The single exit for "the architect could not close it". EVERY such exit comes through
        here, so a decision cannot reach the owner without the PM having seen it first — the early
        return on a failed architect round used to skip the triage entirely."""
        if self._human_brief and self._pm_triage(run):
            ruling = run.dir / "pm-ruling.md"
            if self._converge(run, [ruling.read_text() if ruling.is_file() else "apply the PM ruling"], ""):
                run.tracer.log(event_detail="pm_resolved")
                return True
        run.tracer.log(event_detail="architect_not_resolved", rounds=MAX_ARCH_ROUNDS,
                       human_brief=self._human_brief or {})
        return False

    def _pm_triage(self, run) -> bool:
        """Route the architect's surfaced decision to the product manager. True iff the PM RULED it
        (and wrote the ruling to canon); False if the PM judged it the owner's call, in which case
        the escalation continues carrying the PM's sharpened brief."""
        hb = self._human_brief or {}
        (run.dir / "pm-question.md").write_text(
            f"# Decision routed from the architect\n\n**Q:** {hb.get('question','')}\n\n"
            f"**Why it surfaced:** {hb.get('why','')}\n\n**Options considered:** {hb.get('options',[])}\n\n"
            f"**Architect's recommendation:** {hb.get('recommendation','')}\n")
        pm = self._agent(run, "product-manager", "architect",
            intent="Rule the architect's product question, or confirm it is the owner's call",
            prompt=("The architect could not resolve a finding without a PRODUCT judgment and routed it "
                    "to you — read pm-question.md (added run dir) and context.md for governing canon. "
                    "Decide whether this is YOURS (a routine product call that follows from existing "
                    "canon and strategy) or the OWNER'S (pricing, legal/compliance, business model, "
                    "risk posture, a net-new bet). If it is yours, RULE it by authoring the smallest "
                    "canon clarification that removes the ambiguity — never by weakening an invariant, "
                    "AC or test — and set disposition 'resolved'. If it is the owner's, set "
                    "disposition 'escalate' and sharpen human_brief so they can rule it in one read. "
                    "When unsure, escalate. Return your envelope."),
            cwd=run.workspace, add_dirs=[run.dir], gate_fns=[])
        if pm is None:
            self._last_breach = []          # a PM breach must not masquerade as a ruling
            run.tracer.log(event_detail="pm_unavailable")
            return False
        findings = [f for f in (getattr(pm, "findings", []) or [])]
        resolved = bool(findings) and all(
            getattr(f, "disposition", "") == "resolved" for f in findings)
        pm_brief = getattr(pm, "human_brief", {}) or {}
        if pm_brief.get("needed"):
            # The PM says this is the owner's. Its brief supersedes the architect's — it is the
            # product voice, and it has already filtered out everything it could rule itself.
            self._human_brief = pm_brief
            run.tracer.log(event_detail="pm_escalated", question=pm_brief.get("question", ""))
            return False
        if not resolved:
            run.tracer.log(event_detail="pm_inconclusive")
            return False
        run.commit(getattr(pm, "commit_message", None) or f"pm({self.journey}): rule product decision")
        (run.dir / "pm-ruling.md").write_text(
            "# The product manager ruled this decision\n\n"
            + (getattr(pm, "remediation_brief", "") or "")
            + "\n\n" + "\n".join(f"- {getattr(f,'ref','')}: {getattr(f,'action','')}" for f in findings))
        run.tracer.log(event_detail="pm_ruled",
                       refs=[getattr(f, "ref", "") for f in findings])
        self._human_brief = None            # ruled, so nothing is owed to the owner
        return True

    def _retest(self, run) -> bool:
        """The stale-green-light guard (SSSF §6.5, ported 2026-08-20).

        A review may only accept a tree whose suite is green RIGHT NOW. Both accept-on-review paths
        in the architect loop could return True on a tree whose last recorded unit result was RED:
        `_converge` exits when its final iteration fails, and a fresh review of that same tree was
        enough to accept it. Tests and approval must describe the same tree, so the suite is re-run
        as a code phase before any review-only acceptance."""
        rep = gates.cmd_gate("test:unit", "npm run test:unit")(None, run)
        run.tracer.gate(rep)
        run.tracer.log(event_detail="retest_before_accept", passed=bool(rep.passed))
        return rep.passed

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
