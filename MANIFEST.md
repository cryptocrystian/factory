# Saipien Labs — Software Factory

**The studio's agentic delivery engine.** Cost-of-goods, not internal tooling: this is the machinery
that performs most billable development work. It is the durable, compounding asset that carries across
ventures — venture #7 is cheap *because* this exists.

One inversion governs everything: **agent proposes, code disposes.** Control flow is code; agents are
called by it and never drive it.

---

## Layout

| Path | What it is |
|---|---|
| [`canon/`](canon/) | The factory's **own** canon — the invariants (I1–I13) as hardstops, and its authority order. The factory is held to the same standard it enforces (G2). |
| `control-plane/` | Deterministic core: phases, typed envelopes, gates, permissions (post-hoc write-enforcement), tracer, budget. No agent in this loop. |
| `lanes/` | The workflows: chore, feature, sweep, bootstrap, escape-triage. Thin scripts; all logic lives in `control-plane/`. |
| `agents/` | Per-role system prompts (`system.md`), frozen and versioned for prompt-cache stability. |
| `adapters/` | The Agent port. `agent_omp.py` drives Oh My Pi in constrained-executor mode. |
| `runs/` | Run records + SQLite trace (gitignored). The raw audit record. |
| `registry.yml` | Which portfolio repos opt into which lanes. |
| [`design/`](design/) | The design corpus — the reasoning behind this system. Reference, not runtime. |

## Design corpus (read order)

1. [`design/AGENTIC-SOFTWARE-FACTORY-PLAN.md`](design/AGENTIC-SOFTWARE-FACTORY-PLAN.md) — **Rev 4, the design of record.** Invariants, architecture, phases, governance.
2. [`design/SOFTWARE-FACTORY.md`](design/SOFTWARE-FACTORY.md) — the origin analysis and the solo-studio reframe.
3. [`design/super-simple-software-factory-implementation-guide.md`](design/super-simple-software-factory-implementation-guide.md) — the concrete `sssf` reference implementation being adapted.
4. [`design/FACTORY-HARNESS-SPEC.md`](design/FACTORY-HARNESS-SPEC.md) — the harness decision and the OMP-constrained Agent-port adapter spec.

## Harness & models (see the harness spec)

- **Control plane:** adapted from disler's `sssf` (`adw_modules/`).
- **Agent port:** **Oh My Pi (OMP)**, run in constrained-executor mode — capability used, autonomy
  rejected (no plan-yolo / prewalk / advisor inside a run). The real boundary is the tool allowlist
  plus post-hoc write-enforcement, not the harness approval prompt.
- **Models:** per-role via OMP `modelRoles`. Building is capability-bound (strongest model); judging is
  correlation-bound (**different families** for reviewer, test-author, and the L4 panel).
- **Economics:** subscription-first (Anthropic OAuth for token-heavy roles), metered only for the short
  cross-family judgment calls.

## Standing constraints

- **The workstation is a client, not a worker.** WSL2/Docker is unreliable under sustained load. Heavy
  and parallel execution runs remote. Local is permitted only for single-stream L0–L2 — which includes
  building the factory itself.
- **Greenfield only.** Existing repos are separate problems, handled individually.
- **Factory machinery is not writable by any agent operating within a run (I12).** This repo is that
  machinery; it is never inside a run's workspace.

## First project

**Arxus** (`~/projects/arxus`) — a business-acquisition marketplace; canon complete through DEC-051.
Currently at **P0**: one lane run by hand, every artifact to a file, before automating.
