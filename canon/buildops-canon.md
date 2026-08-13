# BuildOps Canon — the collaboration & lifecycle layer

The governing spec for the layer that sits **between** the factory kernel (`canon/invariants.md`) and the
product surface (SLATE-OS). It defines how Saipien Labs runs the full project lifecycle — ideation to
post-launch — as concurrent, human+agent, heavily-automated workstreams on one signed event log. The
factory + Buzz are the **runtime** for two SLATE-OS operating tracks that were named and deferred:
**BuildOps** and **StudioOps**. This canon is the source of truth the channel provisioner, templates,
Notifier routing, and (eventually) the SLATE-OS BuildOps views read from.

Resolved through design review (2026-08-12). Supersedes conflicting naming in SLATE-OS canon (§4, §9).

---

## 1. Three panes, one substrate

| Pane | Role | Owns |
|---|---|---|
| **Buzz** | Collaborate & decide | The signed event log: messages, decisions, approvals, stage transitions, agent activity |
| **The factory** | Produce | Build runtime: runs, gates, canon bindings, merges, deployments |
| **SLATE-OS** | Operate & view | The product surface — a read/operate console over the runtime, never a second copy of state |

All three settle into **one signed event log** (Buzz/Nostr) that SLATE-OS reads. Three panes, no more:
Buzz (decide) · Observatory (forensics) · SLATE-OS (operate).

---

## 2. Systems of record — the split-brain hardstop

**One owner per fact.** No entity's authoritative state is writable in two places; there is no two-way
sync. This mirrors the decision SLATE-OS already made for Attio (doc 42): an external system is read-only
context; the owner is canonical; the rest project a cached read.

| Owner | System of record for | Everyone else |
|---|---|---|
| **Attio** | CRM relationship graph — companies, people, deals, pipeline context | read-only into SLATE (one-way) |
| **Supabase (SLATE)** | Advisory execution — engagements, findings, reports, proposals | SLATE authors; others read |
| **Buzz event log** | Collaboration & decisions — messages, approvals, transitions, agent activity | factory/SLATE read |
| **The factory** | Build/stage/run/deployment state | SLATE holds a thin link + cached read-projection |

**Rule:** SLATE-OS holds at most a link + cached projection of build state (the literal Attio precedent —
one id column + on-load fetch). Any SLATE build table is a **read-model, never an authoring surface.**

---

## 3. Ontology

### Entities
| Entity | Is | System of record |
|---|---|---|
| **Tenant** | An isolation boundary = a Buzz community. Saipien-internal is one tenant; each isolated client is its own | Buzz / infra |
| **Account** | A CRM relationship entity (a client org) | Attio |
| **Initiative** | The universal spine: a durable body of work with a full lifecycle (ideation→operate) | SLATE (record) + factory (build state) |
| **Track** | A concurrent workstream/department (a category); realized as a channel | — (category) |
| **Phase** | An Initiative-level milestone (Explore→Shape→Build→Launch→Grow) | Orchestrator |
| **Build** | A concrete release effort under an Initiative's Product track | Factory |
| **Run** | One factory execution/iteration (a lane run) | Factory |
| **Deployment** | Shipping a Build to an environment | Factory |
| **Deliverable** | An advisory artifact (report, proposal, SOW) shared with a client | SLATE |
| **Task** | A discrete work item | Buzz (issues) |
| **Channel** | A Buzz room realizing a Track for an Initiative | Buzz |
| **Canon** | Authoritative ground-truth, polymorphic by track (dev=decisions/invariants; GTM=positioning/ICP; brand=tokens/voice) | the track's repo/SoR |
| **Identity** | An npub — Human or Agent — a Member of Channels with a Role | Buzz |

### Relationships & the three resolved seams
- **Tenant ≠ Account.** Different concepts; a client Account is *promoted* to its own Tenant only when it
  needs hard isolation. Until then, client work is Initiatives inside the Saipien tenant.
- **Initiative is the spine.** Engagement, Build, and Deliverable are *produced under* an Initiative per
  phase/practice — never beside it. SLATE's top-level "Engagement" = the advisory arc of a client Initiative.
- **Phase ≠ Stage.** Phase is the Initiative-level milestone; Stage/Step is a within-track sub-step
  (SLATE's engagement Setup→…→Proposal nests inside the Product track). A Phase contains Steps.
- **One entity, practice-varying label.** Internally everything is an `Initiative` (DRY, one pipeline);
  it *renders* as **"Engagement"** in Advisory, **"Build"/"Project"** in Dev, **"Venture"** in Studio.
  Practice is an attribute (reuse SLATE's `practice_area`), not a separate ontology.

---

## 4. Naming standard (all surfaces)

| Canonical noun | Supersedes | Note |
|---|---|---|
| **Initiative** | "Project" | The lifecycle spine. Materialize SLATE's reserved `Project`; rename the lead-source "New Project" |
| **Build** | — | Keep; the execution unit (maps to `/app/builds`) |
| **Run** | "Sprint" | The factory's iteration unit ("AI Opportunity Sprint" stays an advisory *product*) |
| **Deployment** | "Delivery" (for software) | Reserve "Delivery" to advisory deliverable-sharing only |
| **Deliverable** | — | Advisory artifact |
| **Task** | — | Discrete work item |

- **Channel = `<initiative>-<track>`** (the community *is* the tenant, so names stay short internal or
  customer-facing). Tracks: `strategy · product · gtm · marketing · sales · ops`. Workspace-level:
  `announcements · ops · escalations`.
- **Identifier rule:** human names are `<initiative>-<track>`; the **stable id is a UUID**; a registry
  maps `(tenant, initiative, track) → UUID`. **Automation routes on the registry, never by parsing names.**

---

## 5. Lifecycle — concurrent tracks × phases

Tracks run **in parallel**; phases are **synchronization milestones**, not serial steps. An Initiative
*has* a phase and *runs* all its active tracks at once.

**Tracks (concurrent, each a channel + roster):** Strategy · Product *(the factory)* · GTM · Marketing · Sales · Ops.
**Phases (milestones):** Explore → Shape → Build → Launch → Grow.

| | Explore | Shape | Build | Launch | Grow |
|---|---|---|---|---|---|
| **Strategy** | validate thesis | define bets | guard scope | — | next-cycle theses |
| **Product** | feasibility | spec+design+canon | factory builds+QA | deploy | iterate |
| **GTM** | — | ICP, positioning, pricing | messaging | launch messaging | refine by segment |
| **Marketing** | — | brand direction | content, site, assets | campaigns fire | scale demand gen |
| **Sales** | — | — | pipeline, warm list | activate outreach | close + expand |
| **Ops** | — | — | runbooks | go-live ops | support, retention |

- **Lazy instantiation.** A new Initiative provisions only its **home channel (`<init>`) + `<init>-product`**.
  Other track channels are created when the work and the people are real. Strategy lives in the home
  channel by default. The matrix is the *map*, not a build spec — instantiate a channel and an agent at a time.
- **Phase-gates.** The orchestrator flags a milestone (e.g., Build→Launch) ready only when the *required
  tracks* are ready; it @mentions the tracks, seeds Tasks, and dispatches what's automatable. It never
  auto-advances a gate (§8, B8).

---

## 6. Agents

- **Scoped by channel via templates.** A channel template binds a channel to an agent roster
  (personas → models), so "different models/harnesses per channel" is declarative and a human joining a
  stage finds it pre-staffed. Default roster: **Copilot** (cross-cutting, via `buzz-acp` + Claude Code),
  **Product/PM**, **GTM**, **Strategy**, **Builder/Reviewer** (the factory's own roles).
- **Human-guided.** Agents propose; humans and gates dispose. Honors SLATE's invariant that AI output is
  never final by default. The factory runs on Claude/OMP; SLATE's *product* AI is OpenAI — keep distinct.

---

## 7. Context & durability

Durability *improves* over the prior file-based approach, provided two disciplines hold:
1. **Canvas = the durable anchor** on every channel — canon pointers, current phase, key decisions.
2. **Canon stays authoritative.** Buzz makes *collaboration* context durable (permanent signed log +
   search); it does **not** replace canon. Agents get "full context" by *composing*: authoritative canon
   (bound per task via the Canon port) + channel history/canvas + agent memory + build state.
3. **Curated bundles, not the firehose.** Every agent invocation receives an assembled slice (canon +
   relevant thread + canvas + memory), never the raw channel.
4. **Agent memory (engrams)** enabled so learnings persist across sessions/bodies.

---

## 8. Invariants (B-series) — hardstops for this layer

| # | Invariant | Enforced by (target) |
|---|---|---|
| **B1** | The factory (+Buzz) is the system of record for build/stage/run/deployment state. SLATE never authors it. | SoR canon (§2); SLATE build tables are read-models only |
| **B2** | One writer per fact. No authoritative state is writable in two places; no two-way sync. | Ownership table (§2); review of any SLATE write path touching build state |
| **B3** | Initiative is the universal spine. Engagement/Build/Deliverable are produced *under* it, never beside it. | Ontology (§3); data model |
| **B4** | Tenant (isolation) ≠ Account (CRM). A client Account is promoted to a Tenant only for hard isolation. | Ontology (§3); provisioner |
| **B5** | Phase (initiative milestone) ≠ Stage (within-track step). A Phase contains Steps. | Ontology (§3); UI + orchestrator |
| **B6** | Tracks instantiate lazily: a new Initiative provisions only home + product; other channels on demand. | Provisioner default (§5) |
| **B7** | The stage-gate engine is lifecycle *orchestration*, never project management. Factory owns task execution; no Kanban/assignee/velocity surfaces. | Orchestrator scope; SLATE non-goal |
| **B8** | No gate auto-advances across a human decision. The orchestrator flags readiness; a human rules. | Orchestrator; decisions never auto-dispatched |
| **B9** | Every channel carries a durable canvas anchor; agents receive a curated context bundle, never the raw channel. | Templates (canvas); copilot/ACP context assembly |
| **B10** | Identity is uniform (npub → Human\|Agent → Member+Role); canon is authoritative and bound per task; Buzz references canon, never replaces it. | Buzz membership; Canon port |
| **B11** | Naming is standardized across all surfaces (§4); automation routes on the registry, never by parsing names. | `taxonomy.yml`/registry; provisioner + Notifier |

---

## 9. Authority & relationship to SLATE-OS canon

- This canon governs the collaboration/lifecycle layer and is read by the factory runtime. Within the
  factory, authority order is: `canon/invariants.md` (I-series) > this file (B-series) > `MANIFEST.md` >
  the design corpus.
- It **supersedes** conflicting naming in SLATE-OS canon (§4). Pending SLATE-side actions when we touch
  that repo: (a) add a **BuildOps System-of-Record doc** mirroring the Attio doc (42) that re-declares the
  factory as SoR and SLATE as read-only visualizer; (b) materialize `Project`→`Initiative` + the
  practice-label rule; (c) reserve "Delivery" to advisory deliverable-sharing.
- A machine-readable companion (`taxonomy.yml`) derives the tracks, phases, default channel set, and
  templates from this document; the provisioner and Notifier consume that, this canon is the human source.
