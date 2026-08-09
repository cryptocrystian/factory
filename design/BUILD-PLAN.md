# Factory Build Plan — from hand-cranked to automated

**Date:** 2026-08-09 · **Owner of execution:** the assistant (managing the sequence, reporting at gates).
Grounded in `AGENTIC-SOFTWARE-FACTORY-PLAN.md` (Rev4 §16), `super-simple-software-factory-implementation-guide.md`
(§14 build order), `FACTORY-HARNESS-SPEC.md` (the verified OMP adapter contract), and `P0-LOG.md` (what
the by-hand run taught).

## The shift

P0 ran the full model **by hand** — I issued ~15 OMP calls, ran gates in a shell, committed by hand,
enforced the write-grant by eye. That was correct for P0 (make the design legible). It is **not a
factory.** This plan builds the deterministic control plane so that **one command runs
resolve → plan → build → test → verify → review → finish**, with gates, write-enforcement, tracing,
budgets, and phase-boundary commits owned by code. I1: control flow is code; agents are called by it.

Everything I did by hand in P0 maps to a module:

| By hand in P0 | Becomes |
|---|---|
| Assembled the planner's `context.md` from canon | `canon.py` — binding resolution |
| Built OMP flag sets, piped stdin, parsed the last-assistant envelope, resumed with `-r` | `omp.py` — the Agent port |
| Ran `check:tokens`/`typecheck`/`test:unit`, read pass/fail | `gates.py` |
| `git status` write-grant check, revert on breach | `permissions.py` (I6) |
| `git commit` between phases | `session.py` phase-boundary commits (the #1 P0 lesson) |
| Tracked cost/files/sessions across phases | `tracer.py` + `budget.py` |
| Chained plan→build→test→review, routed failures | `lanes/feature.py` |

## Architecture

**Runtime:** Python 3 + `uv` PEP-723 scripts (matches `sssf`; `uv` is installed). The control plane is
plain modules under `~/factory/control-plane/`; lanes are thin scripts under `~/factory/lanes/` that
sequence phases. Agents are OMP subprocesses driven by `omp.py`. State moves through the filesystem
(`runs/<id>/`) and git; nothing rides a long agent conversation.

```
~/factory/
  control-plane/
    types.py         EnvelopeBase + PlanOutput/BuildOutput/TestOutput/ReviewOutput; AgentCall; PhaseParams; GateReport
    omp.py           the Agent port: flags per role, subprocess, JSONL stream -> tracer, envelope parse, -r resume, usage/cost
    config.py        modelRoles (opus-5 build; gpt-5.6 test/review), per-role tool allowlists, load registry.yml
    canon.py         binding resolution: journey -> {AC, entities, invariants, schema DDL, decisions, L0 tokens} -> context bundle
    session.py       run dir + Phase context manager; entry/exit; COMMIT at each phase boundary; idempotency by run-id
    tracer.py        SQLite (WAL) + files: phases, events, envelopes, gate_results, processes; usage per phase
    permissions.py   snapshot/diff/rollback write-enforcement (I6) via git; per-role writes grant; breach aborts phase
    gates.py         artifacts_exist, files_non_empty, diff_matches_claims, verdict_consistent, cmd_gate(check:tokens|typecheck|test:unit|next build)
    budget.py        per-run token/time/retry ceilings (I9); --max-time; exhaustion halts + escalates
  lanes/
    feature.py       resolve -> plan -> commit -> build -> [verify L0/L1 -> fix]bounded -> test-author -> review -> [revise]bounded -> retest -> finish(accepted)
    chore.py         plan -> build -> verify (T0/T1)
  agents/            frozen system.md per role (planner/builder/test-author/reviewer — already written in P0)
  runs/<id>/         envelopes, sessions, sqlite, diffs (gitignored)
  registry.yml       portfolio repos + lane opt-ins
```

## Build phases (dependency-ordered; each has a falsifiable exit test)

**K1 — Handoff + adapter spine.** `types.py`, `omp.py`, `config.py`, minimal `tracer.py` (JSONL first,
SQLite in K4), `session.py` with phase-boundary commit.
*Exit:* a one-phase lane runs an OMP agent with a role model + tool allowlist, parses a typed envelope,
writes it to `runs/<id>/`, and commits — reproducing the P0 smoke result from code, not by hand.

**K2 — Gates + write-enforcement.** `gates.py` (the four structural gates + `cmd_gate`), `permissions.py`
(snapshot/diff/rollback via git; per-role `writes`).
*Exit:* a builder phase writing outside its grant is rolled back and the phase fails (inject a
`tests/` write into a builder run and watch it revert); a red `cmd_gate` blocks advance.

**K3 — Binding resolution.** `canon.py`: given a journey id, resolve AC + bound entities + invariants +
schema DDL + governing decisions + L0 tokens into the context bundle the planner/builder consume.
*Exit:* `canon.py resolve JRN-S1` reproduces (≥) the `context.md` I assembled by hand in P0.

**K4 — The feature lane + tracer/budget.** `lanes/feature.py` wiring the full chain with bounded fix and
revise loops, phase-boundary commits, the retest guard, and `finish(accepted=…)`; `tracer.py` to SQLite
(WAL); `budget.py` ceilings.
*Exit — the golden run:* `feature.py arxus JRN-S1` runs the whole model automatically and **reproduces
the P0 outcome**: L0/L1 green, the reviewer's DEC-052 blocking finding surfaced, write-grants enforced,
every phase committed, one acceptance decision, full trace queryable. This is the regression benchmark.

**K5 — Close JRN-S1 to accepted (first real automated deliverable).** Run the fix loop through the lane:
builder fixes the ratified-gate ordering + Playwright config; test-author adds the bypass test; reviewer
re-approves.
*Exit:* `feature.py` returns `accepted=true` for JRN-S1 with the reviewer's blocking finding closed.

**K6 — Second journey, hands-off.** Point the lane at a new journey (JRN-B3 NDA→docs or JRN-S4 listing
version) and run it end-to-end with **no hand-driving** — the true test that this is a factory.
*Exit:* a journey neither planned nor built by hand reaches a verdict via one command.

### Later (Rev4 P4–P7 — planned, not now)
Remote/containerized execution (workstation stays a client); parallel lanes + decomposition gate + WIP;
the L3 walker + L4 consensus panel; orchestrator/intake/router; conformance sweep + evaluation harness;
the outer loop (escaped-defect attribution). Each is a phase in Rev4 §16; K1–K6 is the spine they hang on.

## Invariants each phase makes real

I1 (K1, lanes are code) · I2 (K1, typed envelopes on disk) · I3 (K1/K4, different-family test/review;
builder ≠ tests) · I5 (K4, status defaults fail; one `finish`) · I6 (K2, post-hoc rollback) · I7 (K3,
canon-gap halt) · I8 (K4, provenance in trace) · I9 (K4, budget) · idempotency + **phase-boundary
commits** (K1, the P0 lesson) · I13 outer loop (later).

## How I'll manage it

Build K1→K6 in order, each with its exit test run before moving on. Report at each phase gate (what was
built, exit test result, spend). Use the OMP adapter for agent work; write the control-plane code
directly (it's deterministic and I hold the requirements from P0). The golden JRN-S1 run (K4) is the
regression I re-run on any kernel change. When the kernel can run a fresh journey hands-off (K6), the
factory exists; everything after is capability (remote, parallel, sweep) on a working spine.
```
