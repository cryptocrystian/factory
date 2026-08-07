# Agentic Software Factory — Design & Build Plan

**Revision 4 (validation pass)** · 2026-08-04
A standalone system for producing software with agents. Greenfield only.

---

## 1. The problem

Agentic software delivery fails in a specific, repeatable way, and it is not a model-capability
problem.

An agent produces code that runs. Running is mistaken for correct. Nothing independently checks the
output against the intent it was supposed to implement, because the only artifact of that intent was
a conversation. Conversations are bounded, lossy, and get summarized. Intent degrades while code
accumulates. Each subsequent change is made against the degraded artifact rather than the original
intent. Divergence compounds silently and surfaces only when something visibly breaks — by which
time reconstructing intent costs more than the work did.

Four mechanisms drive this:

1. **Intent is ephemeral.** It lives in a context window, not a versioned artifact.
2. **Verification is self-referential.** The producer defines what "working" means, so tests encode
   the producer's misunderstanding and pass forever.
3. **State is unpinned.** Work happens against "the code," not an immutable reference, so two
   participants can hold contradictory views of reality and both be internally consistent.
4. **A human is the transport layer.** Context moves by transcription — lossy, slow, hard-capped at
   one concurrent stream.

A factory is worth building only if it structurally prevents all four.

---

## 2. Goals and non-goals

**Goals**

- Intent survives arbitrarily long projects without degradation.
- Divergence is detected within one cycle, not one quarter.
- Verification is independent of production at every level, including judgment.
- Concurrency is limited by decomposition quality, not human attention.
- The human decides; the human does not transport, schedule, or transcribe.
- A run is reproducible from `(repo, ref, manifest, canon)` and nothing else.
- The system improves from its own escaped defects.

**Non-goals**

- Requirements discovery, business modeling, competitive analysis.
- Zero human involvement.
- Arbitrary tech stacks on day one.
- Retrofitting existing codebases.

---

## 3. Invariants

| # | Invariant |
|---|---|
| I1 | Control flow is code. Agents are called by it and never drive it. |
| I2 | Every inter-agent handoff is a typed artifact on disk. No agent reads another's conversation. |
| I3 | The producer of an artifact never verifies it and never authors its tests. |
| I4 | Every run pins an immutable base reference, resolved from the remote, before any mutation. |
| I5 | Status defaults to failure. Acceptance is a single explicit decision. |
| I6 | An agent may only modify granted paths, verified after the fact against the filesystem. |
| I7 | A decision not governed by canon halts the run. Agents never improvise governance. |
| I8 | Every claim carries provenance: verified, asserted, or unverified. |
| I9 | No run exceeds its declared budget of time, tokens, or retries. |
| I10 | Canon is the only source of intent. If it isn't in canon, it isn't a requirement. |
| I11 | Work verified against a superseded base is never merged. |
| I12 | The factory's own machinery is not writable by any agent operating within a run. |
| I13 | Every escaped defect is attributed to the gate that should have caught it. |

---

## 4. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  CANON       intent as versioned, addressable artifacts        │
└──────────────────────────┬─────────────────────────────────────┘
                           │ resolved per run, by binding
┌──────────────────────────▼─────────────────────────────────────┐
│  ORCHESTRATOR   intake · classify · route · allocate ·         │
│                 dispatch · collect · escalate                  │
└──────────────────────────┬─────────────────────────────────────┘
┌──────────────────────────▼─────────────────────────────────────┐
│  CONTROL PLANE  phases · gates · retries · budgets ·           │
│                 acceptance · commit · merge                    │
│                 deterministic; no agent in the loop            │
└──┬───────┬───────┬───────┬───────┬───────┬─────────────────────┘
   │       │       │       │       │       │
 ┌─▼──┐ ┌──▼──┐ ┌──▼──┐ ┌──▼──┐ ┌──▼──┐ ┌──▼───┐            ports
 │AGNT│ │ISOL │ │GRND │ │TRACE│ │BUDG │ │INTAKE│
 └────┘ └─────┘ └─────┘ └─────┘ └─────┘ └──────┘
```

| Port | Contract |
|---|---|
| **Agent** | `(context, model, prompt, tools, writes, pool) → typed envelope` |
| **Isolation** | `acquire(ref) → workspace; merge(gated); destroy` |
| **Ground truth** | `read(layer) → evidence + provenance` |
| **Trace** | `emit(event)` append-only, OpenTelemetry-shaped |
| **Budget** | `reserve(run) · charge(phase) · exhausted?` |
| **Canon** | `resolve(binding) → sections + version + freshness` |
| **Intake** | `poll() → work items; ack; defer` — issue tracker, file, API, sweep output |

Nothing above the port line names a product; nothing below it is depended on by the control plane.

**Deliberate couplings, accepted rather than abstracted:** git as the VCS, and a filesystem as the
artifact substrate. Abstracting these would buy nothing and cost clarity.

---

## 5. Canon

```
canon/
├── manifest.md              architecture, scope, authority order
├── ontology/
│   ├── entities.yaml        typed domain model + artifact bindings
│   └── invariants.yaml      relationship rules that must always hold
├── decisions/D-0001.md      one per decision, stable ID, append-only
├── capabilities/C-0001.md   requirement + acceptance criteria + journeys
├── hardstops.md             non-negotiables → generated guardrail tests
├── glossary.md              vocabulary
└── canon.lock.yaml          §id → {version, last_verified_ref, authority_rank, bindings}
```

### Capability schema

```markdown
# C-0001 — <name>

**Bindings:** <artifact patterns this capability governs>
**Decisions:** D-0003, D-0011
**Status:** ratified · **Version:** 2 · **Last verified:** <ref>

## Requirement
<what and why>

## Acceptance criteria
- AC-1 <observable, checkable statement>
- AC-2 <negative / failure-path case>

## Journeys
- J-1 <goal-shaped path with a terminal success condition>

## Out of scope
- <explicit exclusions>
```

Acceptance criteria feed the independent test author. Journeys feed the walker. A capability lacking
either cannot be verified, and the factory refuses to build it.

### Ontology

A typed domain model — entities, relationships, invariants — **bound to code artifacts**. Not a
formal reasoner. A data structure with four jobs.

```yaml
Lead:
  bindings:
    table: leads
    types: [src/types/lead.ts]
    routes: [/api/leads/**]
  fields:
    email: {type: email, required: true, unique: true}
    org:   {type: ref, to: Organization, cardinality: one}
  governed_by: [C-0001, D-0003]
```

1. **Naming authority.** One name per concept. Agents invent synonyms, and that semantic split is
   invisible until two subsystems disagree about what they store. Binding makes it a detectable
   error rather than an archaeology problem.
2. **Binding resolution.** *"Which canon governs this diff?"* becomes a lookup, not a similarity
   search. This is what makes canon retrieval precise at scale and is the ontology's strongest
   justification.
3. **Generated checks.** Cardinality, uniqueness, required-field, and referential rules compile into
   L0 tests.
4. **Fixture generation.** Seed data for L3 environments is generated from entities and invariants,
   so fixtures cannot drift from the model.

**It must be reconciled against code, not merely authored.** A reconciler parses the live schema and
type definitions and reports divergence as *code drifted* or *ontology stale*. An unreconciled
ontology becomes stale canon, which is worse than none.

**Ontology changes are schema changes.** An entity's shape has the widest blast radius of anything
in canon — it invalidates bindings, generated checks, fixtures, and any capability referencing it.
Ontology amendments are governed at the highest tier (§13, G1) and carry a migration obligation:
every affected binding is re-resolved and every generated check regenerated as part of the same
amendment.

Out of scope: OWL/RDF, inference engines, modeling anything without an artifact binding.

### Canon linter

- IDs unique; references resolve; no orphans
- Every capability has ≥1 acceptance criterion, ≥1 negative-case criterion, ≥1 journey
- Every journey declares a terminal success condition
- Authority order is total
- Every ontology entity binds to ≥1 artifact pattern
- No two capabilities claim overlapping bindings without declared precedence

### Freshness

Every section carries `last_verified_ref`, so the conformance sweep can conclude **"canon is stale
here"** rather than only "code drifted." Without this, a sweep eventually reports false divergence,
trust erodes, and the verification layer dies quietly. This is the most common way conformance
systems fail.

---

## 6. The run model

```
resolve       pin origin/<default>; refuse if local diverges; load manifest;
              resolve canon by binding; lint
plan          → PlanOutput      gates: artifacts, canon coverage, decomposition
commit(plan)  code
build         → BuildOutput     gates: diff matches claims, writes honored
test-author   → TestOutput      gates: every AC has ≥1 assertion   [writes: tests/ only]
verify L0–L2  code              bounded fix loop → builder
review        → ReviewOutput    gates: verdict consistent           [writes: none]
walk L3       → WalkOutput      per journey                         [writes: none]
judge L4      → consensus       N evaluators, threshold             [writes: none]
retest        code, if anything changed after the last green result
rebase-check  refuse if base advanced (I11); rebase + re-verify L0–L2
commit(code)  code — only on full verification
document      → DocOutput
merge         gated by risk tier
finish        accepted = f(all gates)
```

**Phases, not conversations.** Each phase is a fresh bounded session receiving exactly the envelope
it needs. There is no long-running context to compact.

**Envelopes are claims; gates are the audit.** Failed claims return to the same session as
corrections, bounded by retries.

**Write enforcement is post-hoc and comparative.** Snapshot the tree before, compare after, roll back
anything outside the grant, fail the phase. Comparison catches reversion — a path modified before and
clean after was reverted, and reversion is modification. A tool allowlist cannot achieve this: shell
runs anything and write reaches any path.

**Write grants:**

| Agent | May write |
|---|---|
| planner | `canon/proposals/` only |
| builder | source, excluding `tests/`, `canon/`, factory machinery |
| test author | `tests/` only |
| reviewer, walker, evaluators | nothing |
| documenter | docs paths only |

The builder's exclusion of `tests/` is what makes I3 real. A builder able to edit tests will
eventually satisfy a failing gate by weakening the assertion, and a bounded fix loop makes that
outcome likely rather than merely possible.

**Canon-gap halt (I7).** A phase encountering a decision canon does not govern emits
`blocked/canon_gap` citing what is missing, and the run halts. No code is written on ungoverned
decisions. The gap escalates for ratification; canon is amended; the run resumes at the same pinned
reference. This converts silent divergence into a loud, blocking event at the moment it would
otherwise begin.

**Staleness (I11).** A run pins its base at start. If the base advances mid-run, the merge gate
requires rebase and re-verification of L0–L2 at minimum.

**Idempotency.** Commit and merge phases are idempotent by construction: each is keyed by run id and
checks for its own prior effect before acting. A run interrupted after a commit and resumed does not
double-commit. This is what makes resumption safe rather than merely possible.

**Envelope schema versioning.** Every envelope carries a schema version. The control plane refuses a
version it cannot parse rather than coercing it, and stored envelopes remain readable across
migrations so historical runs stay auditable.

---

## 7. Verification

### The inner loop — gates

| L | Question | Instrument | Deterministic | Author | When |
|---|---|---|---|---|---|
| 0 | Does it build? Do invariants hold? | lint, types, compile, generated ontology + hardstop checks | yes | code | every phase |
| 1 | Does it do what the code says? | unit tests | yes | builder | every phase |
| 2 | Does it satisfy acceptance criteria? | tests derived from AC-## | yes, once written | test-author agent | every phase |
| 3 | Does the journey work? | agent drives the running app toward J-## as a goal | no | walker | pre-merge, scheduled |
| 4 | Is it sound? | N independent evaluators against canon intent | no | consensus panel | risk-tiered, scheduled |

**L3 discovers; L2 pins.** Scripted tests assert what was anticipated; a goal-driven walk finds what
was not. Everything a walk discovers is promoted to a scripted acceptance test.

**L4 requires consensus.** One evaluator asked whether something is sound produces plausible prose.
N evaluators with a threshold produce signal. Use different model families — correlated blind spots
defeat the mechanism.

**Never instruct an evaluator to report only high-severity findings.** Severity filters are followed
literally and recall collapses. Report everything with confidence and severity; filter separately.

**Flake handling.** Quarantine on detection (N reruns), route to the test author, never the builder.

### The outer loop — escaped defects (I13)

Gates are the inner loop and they cannot improve themselves. The outer loop is what makes the
factory get better rather than merely operate, and it is the single most important control mechanism
in the design after the canon-gap halt.

When a defect is found in already-merged work — by a sweep, by a later run, by use — it is recorded
as a **gate escape** and attributed:

```
defect observed
  → which run merged the change?         (trace lookup by binding)
  → which gates passed it?               (gate evidence for that run)
  → which acceptance criterion should have caught it?
        exists but the test was weak     → test-author feedback; strengthen assertion
        exists and the test was right     → the gate level was wrong; escalate the capability's tier
        does not exist                    → CRITERIA GAP: canon amendment
  → record: capability, gate level, root class, remediation
```

Three properties make this work rather than become a blame log:

- **Attribution is mechanical**, from the trace, not reconstructed from memory.
- **Remediation is a required output.** An escape that produces no new criterion, test, or tier
  change has not been closed.
- **The rate is tracked per capability and per gate level**, so the data answers *where is
  verification weakest* rather than *who erred*.

**Escape rate by gate level is the factory's primary quality metric.** A rising L2 escape rate means
acceptance criteria are degrading. A rising L4 escape rate means the panel is miscalibrated or too
small. Without this loop the factory's quality is fixed at whatever it was on day one.

### Measuring acceptance-criteria quality

Weak criteria produce weak L2 tests — the mechanism by which a large passing suite verifies nothing.
Three controls:

1. **Mechanical** — the linter requires observable phrasing, a negative case, and journey coverage.
2. **Review** — independent criteria review at ratification: is each criterion observable, does the
   set cover the requirement's stated scope, what failure paths are unaddressed.
3. **Empirical** — every L3/L4 finding **and every escaped defect** that maps to no existing
   criterion is recorded as a criteria gap. That rate is the only measure grounded in outcomes. A
   capability with a persistent gap rate needs its criteria rewritten, not more tests.

---

## 8. Isolation and execution topology

### Worktrees and containers

| | Worktree | Container |
|---|---|---|
| Isolates | source tree | source, runtime, network, filesystem, toolchain |
| Cost | milliseconds | seconds to minutes |
| Parallel servers / databases | collide | isolated |
| Blast radius | **whole machine** | container boundary |
| Reproducible | depends on host | pinned |
| Runs remotely | no | yes |

A worktree isolates code, not execution. The moment two streams run a server, a migration, or a
browser walker they collide on ports, databases, caches, and global configuration. Separately, an
agent with shell access in a worktree reaches everything the user can — post-hoc write enforcement
catches repository changes but cannot prevent a network call to a live service.

**The model is a worktree inside a container**, tiered by work class:

| Work class | Isolation |
|---|---|
| Docs, config, pure refactor (L0–L1) | worktree |
| Anything running the app or touching a service (L2–L3) | container |
| Elevated risk tier | container, default-deny egress with allowlist |

### Workstation constraints

The development workstation runs WSL2 with an unreliable Docker Desktop installation, and WSL2 is
prone to failure under sustained load. This is a hard design constraint.

**The workstation is a client, not a worker.** Running concurrent containerized streams — each with
an application server, a database, and a browser walker — on this host is precisely the workload
that destabilizes it. The architecture treats the workstation as an authoring and review surface,
which is the better structure regardless.

```
CLIENT        the workstation
              canon authoring · review · copilot · dispatch · reading traces
              light, interactive, may be offline without stopping work

PERSISTENT    always-on, small (1–2 vCPU)
              intake · queue · orchestrator · trace store · canon index · budget ledger
              the only component that must never be down

WORKER        ephemeral, elastic, remote
              control plane · agents · containers · L3 environments
              heavy, bursty, location-independent
```

**Local execution is permitted only for:** single-stream, L0–L2, no running application — the loop
for building the factory itself, which is light enough that WSL handles it. Everything else runs
remotely.

Practical notes for the local surface:

- Keep repositories on the WSL filesystem, never `/mnt/c` — an order-of-magnitude difference and a
  common cause of apparent agent slowness.
- Cap WSL memory and processors explicitly in `.wslconfig`; the default allocation is what makes
  heavy load fatal rather than merely slow.
- If local containers become necessary, rootless Podman inside WSL avoids the Windows-side daemon
  and is materially more stable. Worth trying; do not build the plan on it.

**Worker substrate.** Start with hosted CI runners: containers native, per-minute billing, zero local
resource cost, elastic. Move to dedicated runners only when throughput or cold-start justifies it.

**The enabling rule, enforced from day one even while running locally:** a run must be reproducible
from `(repo, ref, manifest, canon)` alone — no host state, no local credentials, no manual setup.
Enforced early, relocation is a configuration change; discovered late, it is a rewrite.

---

## 9. Orchestration and the human interface

```
intake      work items from any source (port)
   ▼
classify    ← the only agentic judgment step: capability, risk tier, lane
   ▼
route       ← rules: risk tier × lane → review floor, isolation class, budget
   ▼
allocate    ← WIP limits; decomposition gate; refuse unseparable work
   ▼
dispatch    ← run
   ▼
collect     ← gates
   ▼
escalate    ← anything ambiguous. never "decide anyway"
```

### Flow control

Throughput is governed by queue mechanics, not enthusiasm. Lead time equals work-in-progress divided
by throughput, so uncontrolled WIP inflates lead time without increasing output — the classic
failure of parallel systems.

- **WIP limits** are explicit and enforced: global, per project, and per risk tier.
- **Backpressure.** When workers saturate, intake defers rather than queueing unboundedly. An
  unbounded queue converts a capacity problem into a latency problem that is invisible until it is
  severe.
- **Aging.** Items waiting beyond a threshold escalate rather than starve.
- **Queue depth, wait time, and utilization are first-class metrics** (§14), because bottlenecks
  move and the only way to find the current one is to measure.

### Decomposition gate

A planner proposing parallel streams declares, per stream, the artifact bindings it will touch. The
control plane computes pairwise intersection; overlap beyond a declared threshold refuses parallel
allocation and serializes instead. Post-hoc, merge-conflict rate per run is tracked and fed back — a
planner whose decompositions conflict is producing bad plans, and that is measurable.

### Risk tiers

| Tier | Scope | Review | Isolation |
|---|---|---|---|
| **T0** | No runtime effect: docs, comments, tests | auto-merge on green | worktree |
| **T1** | Application code; no auth, data model, money, external effects | auto-merge on green + clean consensus | container |
| **T2** | Auth, permissions, data model, external integrations, ontology changes | human approval | container |
| **T3** | Money paths, migrations, production config, secrets | human approval + staged rollout | container, egress-restricted |

### What reaches the human

| Never | Triaged, with evidence | Always |
|---|---|---|
| Passing gates | Consensus findings above threshold | Canon ratification |
| Bounded fix loops | Canon-gap escalations | T3 changes |
| Rollbacks on breach | Walker failures | Production mutations |
| T0 merges | New conformance divergence | Secrets |
| Doc generation | Budget exhaustion | Irreversible operations |
| | Escaped-defect attributions | |

Triaged items arrive as an **evidence bundle** — the finding, the canon section it violates, the gate
output, the diff hunk, the confidence — never as a diff to read.

### Reducing ratification cost

Every canon gap routes to one person, which is correct for safety and is the most likely queueing
point. Reduce the cost of each ratification, not the requirement:

- The copilot **drafts** the amendment — affected sections, proposed wording, downstream impact by
  binding traversal — so ratification is approve/reject/revise rather than authoring.
- Amendments are **batched** where they do not block a running stream.
- Gaps are **pre-classified** as novel versus covered-by-precedent, with precedent cited.

### Review copilot

An agent that assists review and **cannot approve** — structurally, not by instruction.

| Property | Specification |
|---|---|
| Authority | None. No write grant, no merge capability, no canon write, no run-record write |
| Seeding | Artifacts at the run's pinned ref: cited canon, plan, diff, gate evidence, findings, escalation reason. Never a chat history |
| Grounding | May read the repo at ref, query data sources, re-run any gate, dispatch a walker. It investigates; it does not speculate |
| Provenance | Every claim tagged verified / asserted / unverified (I8) |
| Model | Different family from that run's builder and reviewer |
| Lifetime | Per review; dies with the decision |
| Output | Advisory notes on the review record. The human's decision is what is recorded |

---

## 10. Durable context

| Component | Purpose |
|---|---|
| Addressable canon | Intent survives sessions |
| Binding-based retrieval | Resolve governing canon per diff, deterministically |
| Freshness tracking | Distinguish code drift from stale canon |
| Append-only decisions | Provenance in one hop |
| Conformance sweep | Continuous divergence detection |
| Escaped-defect corpus | The outer loop's memory (§7) |
| Pattern library | Cross-project learning, stamped at bootstrap |
| Failed-run corpus | Failed runs contain information: plan, gate output, failure mode |

**Conformance sweep.** Scheduled, per project. For each canon section:
`verified | drifted | unknown | canon-stale`, at a pinned ref, with evidence. The matrix only grows —
findings are additions, never resets — so confidence cannot collapse and restart. On greenfield it
starts green, which is the point: the first divergence is loud.

---

## 11. Safety and controls

| Control | Mechanism |
|---|---|
| **Write scope** | Post-hoc comparative enforcement with rollback |
| **Secrets** | Never in the workspace. Injected per phase, scoped to that phase, redacted from traces. Elevated tiers get short-lived credentials or none |
| **Network egress** | Default-deny at elevated tiers; allowlist per manifest |
| **Budget** | Per-run ceilings on tokens, wall-clock, retries. Exhaustion halts and escalates |
| **Kill switch** | Process registry per run. A hung agent emits nothing, so pid tracking is the only way to stop it. Children before parents |
| **Irreversibility** | Operations that cannot be undone require explicit approval regardless of tier |
| **Model pinning** | Versions pinned per role. A model change is a deliberate re-baseline with evaluation, not an ambient event |
| **Prompt stability** | Kernel prompts frozen and versioned. Caching is prefix-matched; any change silently multiplies cost. Verify cache-read tokens are non-zero across repeated runs |
| **Dependency additions** | Separate gate: license, provenance, transitive delta |
| **Factory machinery** | Not writable by any agent in a run (I12) |
| **Schema evolution** | Envelope, canon, and trace schemas are versioned with forward-compatible readers and explicit migrations. Historical runs remain auditable across changes |

### Evaluation harness

The factory is software and its behavior changes when prompts, models, or gates change. Without a
regression set, every tuning change is a guess.

A fixed corpus of work items with known-good outcomes, re-run on any change to prompt, model, gate,
or tier policy. Measured: acceptance rate, gate pass/fail distribution, cost per run, wall-clock, L3
finding rate, criteria-gap rate. A change that improves one metric while silently degrading another
is the normal case, which is why this cannot be judged by impression.

---

## 12. Model strategy and economics

### The premise

Not one model versus another — one model **and** another, where the failure modes differ.

| Failure mode | What it looks like | Response |
|---|---|---|
| **Capability** | The model cannot do the task well enough | Strongest available model, higher effort |
| **Correlated error** | Confidently wrong in a way another instance of itself repeats | A different model family |

**Building is capability-bound. Judging is correlation-bound.** Model diversity applied to
construction is cost without benefit; a single family applied to judgment is a blind spot with a
quorum attached.

### Role assignment

| Role | Tier | Effort | Distinct family |
|---|---|---|---|
| Classifier | Fast | low | no |
| Planner | Frontier | high–max | no |
| Criteria reviewer | Frontier | high | from planner |
| Builder | Frontier → sweep down | high → tune | no |
| Test author | Frontier | high | **from builder — strongly preferred** |
| Reviewer | Frontier | high | **from builder — mandatory** |
| Walker | Vision-capable workhorse | medium | optional |
| Consensus panel | Mixed, workhorse | medium | **≥2 families — mandatory** |
| Documenter | Workhorse | low | no |
| Sweep / conformance | Workhorse, batched | low–medium | no |
| Review copilot | Frontier | high | **from that run's builder and reviewer — mandatory** |

The **reviewer** evaluates the same artifact the builder produced; same-family review shares the
misreading that produced the defect. The **test author** is the same argument one layer up: if
builder and test author misinterpret a criterion identically, the test passes and the requirement is
unmet.

**Prompt-lens diversity is not model diversity.** Different lenses on one model catch different
categories but share its blind spots. Useful; not a substitute.

### Capacity: subscription first, metered on overflow

A subscription is a fixed-cost capacity pool that resets on a rolling window. Unused allowance is
wasted money, so the correct behavior is to consume it to a reserve and spill only past that.
Capacity sourcing is a **routing policy**, not an architectural boundary.

| Pool | Capacity | Marginal cost | Families |
|---|---|---|---|
| Subscription | Fixed, window-resetting | ~0 until exhausted | Single vendor |
| Metered | Unlimited | List | Any |
| Batch | Unlimited, latency-tolerant | Discounted | Any |

Each role declares eligible pools in preference order, latency tolerance, and any family constraint.
The broker resolves the pool at dispatch and records which pool served the call.

**Headroom reserve.** A configurable share of the rolling window is reserved for interactive use; the
factory consumes only the remainder. This prevents an overnight sweep from starving the next
morning's work and is the parameter needing the most tuning. Set it high initially.

**Spill, never stall.** When headroom falls below reserve, eligible work routes to metered. A run
never fails because a pool is exhausted; it degrades and the trace records the substitution.

**The irreducible metered spend is small and well-placed.** Roles requiring family diversity —
reviewer, panel, copilot — cannot draw on a single-vendor subscription. But those are short
evaluation calls. The roles dominating token volume (plan, build, test author) are exactly what the
subscription covers, so list price is paid only for cheap calls.

**Cost attribution does not require metered billing.** The Budget port records tokens per phase per
run. Engagement cost is a blended-rate calculation over the trace, not an infrastructure constraint.

**Do not downgrade the subscription preemptively.** Instrument, run a full billing period, measure
consumption and spill rate, then decide at renewal.

### Downgrading by task class

Cheaper models belong where **errors are cheap and detectable** — never on the critical path to
acceptance, where the saving is recovered several times over in failed runs.

| Safe to downgrade | Never downgrade |
|---|---|
| Classification, routing, triage | Planner — upstream errors compound |
| Documentation | Test author — weak tests are indistinguishable from strong until something ships |
| Conformance sweep | Reviewer, consensus panel — the last line |
| Flake detection, log triage | Criteria review — determines whether verification means anything |
| Commit-message synthesis | |

The walker's cost is driven by image tokens; cap steps per journey before reaching for a cheaper
vision model.

---

## 13. Governance

Run-level controls constrain what a run may do. Governance constrains how the system itself changes,
and who decides.

**G1 — Canon authority.** Ratification is a named human decision, always. Path: proposal (drafted by
copilot or agent into `canon/proposals/`) → review → ratification → version increment →
`last_verified_ref` update → affected capabilities re-swept. Authority order in `manifest.md`
resolves doc conflicts and must be total. Superseded decisions are marked, never deleted. **Ontology
amendments are T2 minimum** and carry a migration obligation: every affected binding re-resolved,
every generated check regenerated, in the same amendment.

**G2 — Factory change control.** The factory has its own canon; the invariants in §3 are its hard
stops. Gates, prompts, tier policy, and write grants are protected paths no agent in a run may
modify (I12). Once the bootstrap lane exists the factory should be built by itself — a system that
cannot pass its own gates has no standing to enforce them.

**G3 — Agent roster.** Models pinned per role. Upgrades are deliberate: pin, run the evaluation
harness, compare, re-baseline the sweep, adopt. Prompts are versioned artifacts under the same change
control as code. Roster changes — adding a role, widening a grant, raising a budget — are governance
decisions, not configuration edits.

**G4 — Data.** What may enter an agent context is a policy question with contractual consequences.
Define per project: whether production data may be read, whether personal data may appear in traces
or envelopes, retention, residency. Traces are durable and searchable, which makes them a
data-protection surface — redaction happens at write time.

**G5 — Provenance and IP.** Generated code carries provenance in the trace: model, version, prompt,
governing canon. Dependency additions pass a license and provenance gate. The audit trail must
answer what produced a given line and under what instruction.

**G6 — Approval rights.** Authority is per tier and per project, explicit, and assigned by name where
more than one person can approve. Break-glass exists, requires a stated reason, and is audited — an
override leaving no record is indistinguishable from a bypass.

**G7 — Retention.**

| Artifact | Retention |
|---|---|
| Canon, decisions, ratifications | Permanent, versioned |
| Accepted-run envelopes, gate evidence, acceptance decisions | Permanent |
| Escaped-defect records and attributions | Permanent |
| Full traces, raw agent output | Bounded window, then pruned to summary |
| Failed-run corpus | Bounded window; failure mode and plan retained permanently |
| Ephemeral workspaces | Destroyed on completion |

**G8 — Audit.** Every acceptance decision must be reconstructible from artifacts alone: what was
asked, which canon governed it, what was built, what verified it, what evidence existed, who approved
it, against which reference. If a decision cannot be reconstructed without a person's memory, the
system has regressed to §1.

---

## 14. Operations and reliability

A factory that cannot be operated unattended is a script with good intentions. These are the controls
that make unattended operation real.

### Service levels

| Objective | Why it matters |
|---|---|
| Run acceptance rate | Falling rate signals model, prompt, or criteria degradation |
| Queue wait time (p95) | The user-visible latency of the whole system |
| Dispatch liveness | Work queued with no dispatch is the silent-stall failure |
| Canon freshness | No section unverified beyond a threshold age |
| Escaped-defect rate by gate level | The quality signal (§7) |
| Persistent-tier availability | The only component that must not be down |

Without stated objectives there is no way to distinguish *degraded* from *normal*, and every
investigation starts from zero.

### Alerting

Observability without alerting is forensics. Three severities:

| Severity | Conditions |
|---|---|
| **Page** | Persistent tier down · queue has work but no dispatch for N minutes · spill rate exceeds threshold (cost runaway) · repeated budget exhaustion |
| **Notify** | Run failed after retries · canon gap awaiting ratification beyond threshold · sweep found new divergence · escaped defect recorded · WIP limit saturated |
| **Digest** | Daily: throughput, acceptance rate, cost per accepted change, queue health, canon freshness |

The distinction that matters: a page means the factory has stopped producing; a notification means it
needs a human decision; a digest means nothing is wrong.

### Metrics

Delivery metrics are standard for a reason — they are the ones that predict whether a pipeline is
actually working.

| Class | Metrics |
|---|---|
| **Delivery** | Lead time (intake → merge) · throughput (accepted changes / period) · change failure rate · time to remediate |
| **Quality** | Escaped-defect rate by gate level · criteria-gap rate · consensus disagreement rate · flake rate |
| **Flow** | Queue depth · wait time · WIP · worker utilization · merge-conflict rate |
| **Economics** | Cost per accepted change · spill rate · cache hit rate · budget exhaustion rate |
| **Canon** | Sections verified (%) · mean staleness age · ratification latency · amendment rate |

**Cost per accepted change, not per run.** A cheap run that fails costs more than an expensive one
that ships. A capability with persistently high cost-per-accepted-change almost never has a model
problem — it has weak acceptance criteria, so runs fail late and repeat. Read together with the
criteria-gap rate.

### Factory self-monitoring

The persistent tier is monitored like any production service: liveness, queue depth, dispatch
latency, error rate, disk. It is small, and it is the single point of failure by design — which makes
watching it cheap and necessary.

### Disaster recovery

Canon and the acceptance record are the durable assets. Everything else is rebuildable.

| Asset | Recovery |
|---|---|
| Canon, decisions | Git, mirrored to a remote. RPO ≈ 0 |
| Acceptance records, gate evidence, escape corpus | Backed up on write; permanent retention |
| Full traces | Backed up periodically; loss degrades analytics, not correctness — the files are the raw record |
| Queue state | Durable queue; the only genuinely stateful component |
| Persistent tier | Rebuildable from configuration |
| Workspaces | Ephemeral by design; no recovery needed |

**Recovery is tested, not assumed.** Restoring from backup into a clean environment is an exit
criterion for the phase that stands up the persistent tier — a backup never restored is a hypothesis.

---

## 15. Lanes

| Lane | Shape | Notes |
|---|---|---|
| **Feature** | Full run model | Default |
| **Chore** | plan → build → verify | T0/T1 only |
| **Migration** | plan → review → build → verify → staged apply | Forward-only. Never auto-applied to production. Reversibility analysis is a gate |
| **Revert** | identify → isolate → verify → merge | Must exist before it is needed |
| **Dependency** | detect → build → verify → provenance gate | Scheduled |
| **Sweep** | read-only conformance | Scheduled; writes findings, never code |
| **Escape triage** | attribute → remediate → amend | Closes the outer loop (§7) |
| **Bootstrap** | stamp canon skeleton, gates, trace, manifest, CI | Creates projects that start green |
| **Cross-repo** | coordinated runs, joint gate, two-phase merge | Deferred — §18 |

---

## 16. Build sequence

Each phase has a falsifiable exit test.

**P0 — Manual run.** One lane by hand on a small greenfield project; every artifact written to a file.
*Exit:* every artifact exists and each is named with what produced and consumed it.

**P1 — Canon layer.** Schema, linter, bindings, ontology with reconciler, freshness tracking.
*Exit:* the linter rejects deliberately malformed canon; the reconciler detects an injected
schema/ontology divergence and correctly attributes it to code or canon.

**P2 — Control plane.** Phases, typed envelopes with versioning, gates, write enforcement, budgets,
trace, pinning, staleness, idempotent commit. One lane: plan → build → verify.
*Exit:* a real work item completes end to end; an injected type error is caught and repaired in the
loop; an ungoverned decision produces a canon-gap halt; an agent writing outside its grant is rolled
back and its phase fails; a run interrupted after commit and resumed does not double-commit.

**P3 — Verification stack.** Independent test author with restricted writes. Walker with ephemeral
environments. Consensus panel. Criteria-gap tracking. **Escaped-defect attribution and the escape
triage lane.**
*Exit:* the walker finds a real journey defect the scripted suite passes on, and it is promoted to an
acceptance test; an injected escaped defect is correctly attributed to the gate that should have
caught it and produces a remediation.

**P4 — Remote execution.** Containerized runs on remote workers; local restricted to single-stream
L0–L2. Isolation port with worktree-in-container.
*Exit:* a run executes entirely on a remote worker with no local resource use and is reproducible
from `(repo, ref, manifest, canon)` on a fresh worker.

**P5 — Parallelism and merge.** Decomposition gate, WIP limits, backpressure, gated merge by risk
tier, revert lane.
*Exit:* three concurrent streams complete and merge under gate with zero manual coordination; an
injected bad merge is cleanly reverted; saturating the workers produces backpressure rather than
unbounded queue growth.

**P6 — Orchestration, persistence, operations.** Intake, classify, route, allocate, escalate.
Persistent tier. Review copilot. SLOs, alerting, self-monitoring, backup.
*Exit:* a run is dispatched, executed, and gated while the workstation is powered off; an injected
stall pages; **a restore from backup into a clean environment reproduces canon and the acceptance
record**; and one week passes in which the operator never opens a repository to decide what to work
on next.

**P7 — Bootstrap, sweep, evaluation.** Bootstrap lane; scheduled conformance sweep; evaluation
harness.
*Exit:* a new project is created by the factory, starts green, and an injected divergence is caught
by the next sweep with correct attribution; a deliberate prompt regression is caught by the harness.

---

## 17. Validation against requirements

| # | Requirement | Assessment |
|---|---|---|
| 1 | Industrial-grade architecture | **Meets.** Fail-closed defaults, mistake-proofing over detection, stop-the-line halts, full genealogy from intent to artifact, separation of duties at a standard usually seen only under regulatory pressure. The outer loop (§7) supplies the closed-loop control that was the one genuine omission |
| 2 | Scalable by design | **Meets, with known ceilings.** Stateless workers, persistent control plane, explicit WIP limits and backpressure, decomposition gate capping parallelism on evidence rather than optimism. Ceilings are named in §18 rather than discovered |
| 3 | Observable by design | **Meets.** Live event stream, per-phase evidence, gate results with what-was-checked rather than pass/fail, SLOs, tiered alerting, self-monitoring. Trace schema is OpenTelemetry-shaped so the surrounding ecosystem is available without rework |
| 4 | Swappable tooling | **Meets.** Seven ports, none naming a product. Git and the filesystem are deliberate couplings, stated as such |
| 5 | Sound engineering fundamentals | **Meets.** Pinned inputs, hermetic reproducibility, fail-fast, least privilege, separation of duties, idempotent effects, schema versioning, audit trail, change management, tested recovery. Artifact promotion is the one standard practice deliberately not adopted — see §18 |
| 6 | Durable, end-to-end managed context with an ontology | **Meets.** Versioned addressable canon, binding-based retrieval, freshness distinguishing code drift from stale canon, append-only decisions, code-reconciled ontology with governed migration, and the escaped-defect corpus closing the loop from observed reality back to intent |

**Honest characterization:** the design is stronger than typical enterprise practice in separation of
duties, provenance, and intent traceability, because those are what the source failure demanded. It
was thinner in operational readiness — alerting, service levels, recovery — precisely because that is
where the source failure did not press. Revision 4 closes that asymmetry. The remaining risk is not
architectural; it is execution discipline on §18.

---

## 18. Residual gaps

1. **Cross-repo atomicity.** No coherent story. Shape: a meta-run pinning refs across repositories
   with two-phase commit — all-verify, then all-merge. Acceptable while single-repo; blocking later.
2. **Partial acceptance.** A run is accepted or not. The workable compromise is *accept with
   follow-up* — merge what verified, auto-file the residual with its failing context — unimplemented.
3. **Artifact promotion not adopted.** Standard pipelines build once and promote; this rebuilds and
   re-verifies per stage. Correct at this scale and wasteful at larger scale. Revisit when build time
   becomes material.
4. **New-stack onboarding cost.** The manifest schema assumes a stack shape; a different stack
   requires adapter work of unknown size until attempted.
5. **Walker reliability.** The least mature component — non-deterministic, slow, expensive. Expect
   the longest tuning. Start with two journeys, not twenty.
6. **Ontology coverage of non-relational state.** Queues, caches, external systems have no obvious
   reconciliation source.
7. **Ratification remains a single-person bottleneck.** Mitigated by drafting and batching, not
   eliminated. The correct place for a bottleneck and the most likely place to queue.
8. **Evaluation-harness corpus construction.** Manual work with no shortcut; the harness is only as
   good as its work items.
9. **Escape detection depends on something noticing.** The outer loop is only as good as defect
   discovery. Until projects have real use, the sweep and later runs are the only detectors — which
   means early escape rates will understate reality.

---

## 19. Open decisions

1. Canon storage — repository files only, or an indexed store with the repo as source of truth?
2. L3 environment strategy — ephemeral environments per walker, or a pooled instance?
3. Consensus panel composition — which model families for evaluator diversity?
4. Persistent-tier host and worker substrate.
5. Kernel implementation — adapt an existing open control plane, or build against an agent SDK.
   Adapting inherits phases, envelopes, gates, write enforcement, and tracing; building yields exact
   fit at the cost of re-solving them.
6. Where the factory's own code lives, and when it begins building itself.

---

## Greenlight

**Build P0 through P3.** The foundation is sound and further design work returns less than the first
real run will teach.

Conditions attached to later phases, each already an exit criterion above:

- **P3 must include escaped-defect attribution.** It changes what the verification stack records, so
  retrofitting it later means losing the history that makes it useful.
- **P6 must not be declared complete without a tested restore.** A backup never restored is a
  hypothesis.
- **Open decision 5 should be resolved before P2 begins.** It determines that phase's shape and
  probably its duration.

**Immediate next action: P0.** One lane, by hand, on a small greenfield project, every artifact
written to a file. One day. It makes the design legible before any of it is automated and reliably
separates the load-bearing phases from the ceremonial ones.
