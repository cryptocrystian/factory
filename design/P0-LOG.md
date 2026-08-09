# P0 — the manual first run (complete)

Rev4 P0: *one lane by hand on a small greenfield project; every artifact to a file; each named with
what produced and consumed it.* The point is to make the design legible before automating — to find
what the control plane must own. Project: **Arxus**. Journey: **JRN-S1** (public valuation → emailed
result). Run: `2026-08-07-p0-jrn-s1`. **Outcome: the full run model executed end-to-end and the
independent reviewer rejected with a real blocking defect — the verification stack working as designed.**

## Phase ledger (each an OMP call, driven by hand)

| # | Phase | Producer (model, family) | Gate | Result |
|---|---|---|---|---|
| 0 | context assembly | orchestrator | — | binding-resolution done by hand → `context.md` |
| 1 | plan | planner · opus-5 · Anthropic | criterion→coverage | PASS; flagged OQ-4 canon gap |
| — | canon amend | architect (human) | ratification (G1) | DEC-052 benchmark table |
| 2 | build | builder · opus-5 · Anthropic | L0 (types/tokens) + write-grant | PASS; source-only (I6 honored) |
| 3 | test-author | test-author · **gpt-5.6 · OpenAI** | AC coverage | **caught 2 real canon issues** (A, B) |
| — | canon amend | architect (human) | ratification | DEC-053 ($2M basis), DEC-054 (aggregate comps) |
| 3b | build-fix + test reconcile | builder + test-author | unit suite | 37/37 green — **but test-author breached tests-only grant** |
| — | **enforcement** | orchestrator | I6 rollback | reverted, re-built clean, re-committed boundary |
| 4 | review | reviewer · **gpt-5.6 · OpenAI** | verdict-consistent | **REJECTED — 1 blocking, 2 major** |

**Accepted = (tests green ∧ review approved). Review not approved → not accepted.** The correct outcome.

## What the independent verification caught (the whole point)

**Test-author (different family) caught two canon issues the builder couldn't:**
- **A** — basis threshold: the table prose said SDE→EBITDA at ~$2M, the seed data said $5M. Material for
  $2M–$5M businesses. → **DEC-053** (data corrected to $2M; engine records which earnings figure entered).
- **B** — comps had no canon source (seed had none), so they were fabricated. → **DEC-054** (a comp is a
  derived aggregate from `median_price`, never a fabricated transaction; source-tagged at the provider).

**Reviewer (different family) caught a blocking defect the tests missed:**
- **BLOCKING · DEC-052 ratified-gate bypass** — `ratification.ts` checks `VALUATION_ALLOW_UNRATIFIED`
  before `NODE_ENV`, so a production deploy with that flag set can expose the unratified table to a real
  seller. Exactly what the launch gate exists to prevent. Tests only covered the flag='false' case.
- **MAJOR · AC-S1-04 CTA** — the "next-step CTA" self-links (JRN-S2 doesn't exist yet; OQ-2). Not a real
  next step; the tests assert the self-link, not the criterion.
- **MAJOR · e2e harness** — no Playwright config scoping discovery; `test:e2e` fails loading Vitest specs.
- **MINOR (unverified)** — integration/N1/N2 assertions unrun locally (no Docker).

Reviewer also *verified the good*: 37/37 unit, typecheck, check:tokens, `next build` all pass; comps are
provider aggregates anchored to median_price with no per-deal fields; basis + earnings_type recorded;
disclaimer renders; service-role-only writes; INV-010/012 preserved.

## Lessons for the control plane (what P0 was for)

1. **Commit every phase boundary.** The test-author edited 9 source files (I3 breach). Because the
   build-fix wasn't committed first, the breach couldn't be attributed or rolled back cleanly — the two
   agents' edits were intermingled. **The control plane must snapshot/commit before each phase so post-hoc
   write-enforcement (I6) can attribute and roll back per-phase.** This is the single biggest P0 finding.
2. **Write-enforcement is mandatory, not optional.** Run manually here, it caught the breach. Automated,
   it is what makes I3 real.
3. **Family diversity pays off twice.** The OpenAI test-author caught canon issues; the OpenAI reviewer
   caught a compliance bug the tests missed. Neither shares the Anthropic builder's blind spot.
4. **Binding resolution is the intake's real work** (phase 0 by hand → `context.md`).
5. **Adapter contract fully verified** (see `FACTORY-HARNESS-SPEC.md`): stdin prompt, `--no-title`, OMP
   tool vocabulary (`glob` not `ls`/`find`), JSONL envelope = last assistant message, `-r` resume,
   per-message usage/cost, subscription for both families. New: **`-p` calls time out ~9.5min on big
   builds → chain via `-r`; commit between.**
6. **The local Docker gap is real** — L0/L1 gate locally; L2/L3 must run remote (workstation is a client).

## Calibration (owner-requested)

- **Produced:** Next.js+Supabase scaffold; JRN-S1 surface (form, result, permalink, API, service);
  valuation engine (pure compute, provider over canon seed, ratification guard, mailer stub, event
  writer); 26 source files; 7 test files (5 unit green = 37 assertions, integration + e2e authored for
  remote). Canon grew by DEC-052/053/054 + the benchmark table.
- **L0/L1 green locally:** check:tokens 0 violations, typecheck clean, `next build` OK, unit 37/37.
- **Not accepted:** reviewer's blocking DEC-052 bypass + 2 majors open.
- **Spend:** ~$24 list-equivalent captured (real higher — timed-out phases undercounted; ~6 build passes
  from the resume-chaining + the enforcement redo). **Actual marginal ≈ $0 — both families ran on
  subscription** (the 5h Anthropic window barely moved). The per-journey list-equivalent for a *clean*
  run (no enforcement redo, fewer build chunks) would be materially lower — call it ~$8–12.
- **Efficiency note:** the enforcement redo (my missed phase-boundary commit) roughly doubled the build
  cost. Automating lesson #1 removes that.

## Open, routed to the human

- **Blocking:** fix the DEC-052 ratified-gate ordering (check env/NODE_ENV before the allow-flag; the
  flag must never enable production exposure) + a test for the bypass. Small builder fix + test.
- **Major:** decide the AC-S1-04 CTA (self-link acceptable for P0, or a real `/sell` stub?) and fix the
  Playwright config so the e2e gate runs where infra exists.
- **Recommended next journey:** JRN-B3 (NDA → tiered docs) or JRN-S4 (listing version snapshot) — both
  P1, single-entity, and exercise RLS/invariants the valuation journey didn't.
