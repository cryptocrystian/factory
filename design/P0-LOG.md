# P0 — the manual first run (log)

Rev4 P0: *one lane by hand on a small greenfield project; every artifact written to a file; each named
with what produced and consumed it.* The point is to make the design legible before automating — to
separate the load-bearing phases from the ceremonial ones, and to discover what the control plane must
own. Project: **Arxus**. Journey: **JRN-S1** (public valuation → emailed result).

Run id: `2026-08-07-p0-jrn-s1` (artifacts in `runs/<id>/`, gitignored — raw record).

## Phase ledger

| # | Phase | Producer | Consumes | Produces | Gate | Result |
|---|---|---|---|---|---|---|
| 0 | context assembly | orchestrator (by hand) | Arxus canon: JRN-S1, AC-S1-*, Valuation entity, INV-010, DDL, DEC-014/019, tokens | `runs/…/context.md` | — | the binding-resolution the control plane must automate |
| 1 | plan | OMP planner · `claude-opus-5` high | `context.md` + canon (read-only) | `plan.md` (486 lines) + envelope | criterion→coverage | **PASS** (~$1.77, 27 tool calls) |
| 2 | scaffold + build | *(pending)* | `plan.md` | Next.js+Supabase app + JRN-S1 feature | diff-matches-claims, writes-honored | — |
| 3 | test-author | *(pending, ≠ builder family)* | `plan.md` §5 | tests | every AC has ≥1 assertion | — |
| 4 | verify L0–L2 | code | repo | gate reports | fail-closed | — |
| 5 | review | *(pending, ≠ builder family)* | plan + diff | review envelope | verdict-consistent | — |

## What P0 has taught so far (feeds the control plane)

**Binding resolution is the real work of intake.** Assembling the planner's context by hand (phase 0)
made concrete what Rev4's binding-resolution must automate: given a journey, pull its acceptance
criteria, the ontology entities it touches, their invariants/hardstops, the bound schema, the governing
decisions, and the L0 design canon. `context.md` is the worked example the control plane generalizes.

**The Agent-port adapter contract, now verified end-to-end** (details in `FACTORY-HARNESS-SPEC.md`):
- Prompt via **stdin**, not positional; `--no-title`.
- OMP **tool vocabulary differs** from sssf/Pi and validates strictly — no `ls`/`find`; listing is
  `glob`. Read-only role set = `read, grep, glob`.
- `--mode json` is clean JSONL; **the envelope is the last `assistant` message** in `agent_end`
  (bare JSON confirmed); usage/cost itemized per message in dollars; prompt-cache verifiably live.
- `-r <id>` resumes the same session with context intact — the correction loop works.
- `--system-prompt <file>` + `--add-dir <repo>` (read-only canon) + per-role `--tools` all behave.

**The planner honored the invariants unprompted** — planned only what canon requires, refused to
improvise governance (I7/I10): flagged 7 genuine canon gaps as open questions with safe defaults and
single swap-points rather than deciding them. The load-bearing one:

**Open canon gap (would escalate to ratification in the full factory):** OQ-4 — canon supplies no
benchmark multiples/comps/SF structures for the valuation engine, though DEC-019 says P0 launches on
"borrowed industry benchmarks." The plan uses clearly-labeled **seed placeholders** behind the
source-tag seam. For P0-by-hand this is an accepted default; it should be ratified into canon before it
is treated as real intelligence.

## Cost

Plan phase ~$1.77 (opus-5/high). Smoke tests: cents (haiku).
