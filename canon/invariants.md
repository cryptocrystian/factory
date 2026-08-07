# Factory Invariants (I1–I13) — hardstops

The factory's own canon. These are non-negotiable; the factory is built to pass them and has no
standing to enforce standards it violates (G2). Source: `design/AGENTIC-SOFTWARE-FACTORY-PLAN.md` §3.
Each will graduate from prose to a machine-checkable guardrail as the control plane is built.

| # | Invariant | Enforced by (target) |
|---|---|---|
| I1 | Control flow is code. Agents are called by it and never drive it. | Architecture: lanes are code; agents are per-phase calls. No plan-yolo/prewalk/advisor in a run. |
| I2 | Every inter-agent handoff is a typed artifact on disk. No agent reads another's conversation. | Envelope schema + `runs/<id>/` persistence. |
| I3 | The producer of an artifact never verifies it and never authors its tests. | Write grants (builder ≠ `tests/`); reviewer/test-author are different-family phases. |
| I4 | Every run pins an immutable base reference, resolved from the remote, before any mutation. | `resolve` phase; refuse if local diverges. |
| I5 | Status defaults to failure. Acceptance is a single explicit decision. | `run.finish(accepted=…)`; gate `status` defaults to fail. |
| I6 | An agent may only modify granted paths, verified after the fact against the filesystem. | Post-hoc snapshot/diff/rollback (`permissions`), not the harness approval prompt. |
| I7 | A decision not governed by canon halts the run. Agents never improvise governance. | Canon-gap halt → `blocked/canon_gap`. |
| I8 | Every claim carries provenance: verified, asserted, or unverified. | Envelope + trace tagging. |
| I9 | No run exceeds its declared budget of time, tokens, or retries. | Budget port; `--max-time`, retry bounds. |
| I10 | Canon is the only source of intent. If it isn't in canon, it isn't a requirement. | Intake = a completed canon package; binding resolution. |
| I11 | Work verified against a superseded base is never merged. | Rebase-check at merge gate. |
| I12 | The factory's own machinery is not writable by any agent operating within a run. | This repo lives outside every run workspace; protected paths. |
| I13 | Every escaped defect is attributed to the gate that should have caught it. | Escape-triage lane; per-gate attribution from the trace. |

## Authority order (total — highest wins)

1. `canon/invariants.md` (this file) — the hardstops.
2. `MANIFEST.md` — layout, harness, standing constraints.
3. `design/AGENTIC-SOFTWARE-FACTORY-PLAN.md` (Rev 4) — the design of record.
4. `design/FACTORY-HARNESS-SPEC.md` — harness & adapter decisions.
5. The remaining design corpus — reference.
