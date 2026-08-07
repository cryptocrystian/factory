# The Software Factory

**A deep dive on IndyDevDan's AI Developer Workflows + Thread-Based Engineering material, and a plan to build a factory for Saipien Labs.**

Sources — both IndyDevDan (Dan Eisler):

1. [AGENT THREADS. How to SHIP like Boris Cherny. Ralph Wiggum in Claude Code.](https://youtu.be/-WBHNFAB0OE) — 31:00, published **2026-01-12**
2. [FORGET Loop Engineering. Agentic Engineering is about THIS](https://youtu.be/VQy50fuxI34) — 34:18, published **2026-07-13**

Note the order: **threads came first.** The ADW/software-factory video is the build-out of what threads measure. Part 1 below covers the later (architecture) video because it defines what we're building; Part 2 covers the earlier (measurement) video because it defines how we'll know it's working.

Written: 2026-07-29 · Updated: 2026-07-30

---

# Part 1 — AI Developer Workflows (the architecture)

## The thesis in one paragraph

"Loop engineering" is a bad rebrand of the software development life cycle (0:22). The loop — a conditional that routes failure back to a build agent — is *one control-flow primitive* inside something much larger: an **AI Developer Workflow (ADW)**. Naming the discipline after one primitive is like naming it "if engineering" or "throw engineering" (8:55). The unit of work worth your engineering time is the whole workflow: intake → plan → build → validate → review → ship, with engineers, agents, and code each placed where they're strongest.

> "Forget about loop engineering. Focus your valuable engineering time and tokens on building AI developer workflows." — 1:10

## The three actors of value creation (3:39)

Everything in the video reduces to placing three actors correctly:

| Actor | Cost | Reliability | Speed | Determinism |
|---|---|---|---|---|
| **Code** | Zero tokens | Highest | Light speed | Total |
| **Engineers** | Highest | High | Slow | N/A |
| **Agents** | Token cost | Lowest | Medium | None |

> "Everyone in their AI psychosis seems to forget code is fast, always runs the same way unless you tell it not to. And guess what? It costs nothing." — 4:03
>
> "Out of these three, code is the most reliable by miles, followed by engineers, and then agents." — 4:36

This ranking is the load-bearing claim of the whole video. Every later recommendation follows from it: **push work down to code wherever the work is deterministic.**

## The diagram progression

The visual is a single animated Mermaid-style diagram that grows for 28 minutes. Each step adds exactly one node. This is the most useful part of the video — it's a build order.

| Time | State of the diagram | What was added |
|---|---|---|
| 4:52 | `Engineer Prompt → LLM → Engineer Review` | The atom. Every workflow is a composition of this. |
| 5:10 | LLM → **Agent** | "Insert your favorite model — it doesn't matter anymore." |
| 5:35 | + `Lint Code` diamond, fail→build | **The first loop.** This is all "loop engineering" ever meant. |
| 6:28 | + `Format Code` | Second deterministic gate |
| 6:58 | + `Test Code` | Third gate; all failures funnel back |
| 7:47 | lint/format/test collapse into **Test Agent**, + `Ship` | Scale compute to scale impact |
| 8:30 | + **Planner Agent** on the front | Now: plan → build → test → review → ship |
| 9:35 | Fan out to **3 parallel git worktrees**, + `Merge` | Isolation + parallelism |
| 10:45 | Worktrees → **agent sandboxes** | "Worktrees are a great place to start, not a great place to end" |
| 12:20 | + **Kanban board** intake (Support/Product/Engineer) | External work enters the system |
| 13:55 | + **Scout Agent** → **Plan Agent** | Search and planning split across two agents |
| 14:40 | + **CI/CD** with pass/fail routing | |
| 15:30 | **"PRODUCTION IS DOWN"** — a dedicated hotfix ADW | A *second, different* workflow |
| 16:10 | + **Hot Fix Agent** (specialized), + human approve/reject gate | An "agent expert," not a general agent |
| 16:50 | **N sandboxes racing** the same fix; first correct wins | Compute → confidence |
| 18:05 | **The software factory** — router dispatching to Hotfix / Feature / Bug / Chore lanes | |
| 21:42 | Full assembled factory | |
| 22:35 | The agentic layer highlighted above the app layer | |

The structural claim that repeats throughout:

> "You and I always show up at the ends. These are the two constraints of agentic engineering: prompting, also known as planning, and reviewing, also known as validation." — 7:09

## The agentic layer (21:54)

The video's most contested claim:

> "The best engineering teams never touch the product themselves. [...] The best teams are doing meta work on the agentic layer. They're building the system that builds the system."

The distinction: the **app layer** is your product code. The **agentic layer** is the prompts, skills, subagents, system prompts, routers, and gates that wrap it. His argument is that once a product is scaled and has users, all engineering leverage lives in the agentic layer, because a correct workflow gets multiplied thousands of times.

> "Once you get this right, you have a repeatable workflow that you can run tens, hundreds, and thousands of times, delivering consistent results, if you template your engineering into the fabric of your AI developer workflows." — 24:48

And his definition of what separates this from vibe coding:

> "Vibe coding is not knowing how the system works and not looking at how the system works. Agentic engineering is knowing your system works so well you don't have to look." — 25:48

## The three concrete recommendations (26:45 – 31:10)

### 1. KISS — but separate code from agents from day one (27:00)

Start with one agent and one linter. But the separation is non-negotiable:

> "I'm not saying write a skill, have your agent build, and then at the bottom of the skill, run lint. Separate this out. Use an agent SDK, run a build agent, do work, and then run a linter. And when the linter fails, pass that back into the build agent **with the same session ID**. You have to separate your code and your agents. Otherwise you just have an agent calling code. That's not what we want." — 27:15

This is the single most actionable line in the video. The failure mode it names — a mega-skill that orchestrates everything inside one agent turn — is untestable, unobservable, and can't be gated.

> "This is not a big skill where you run a hundred different nodes of workflows. There are massive testing, massive validation problems with doing that." — 27:45

### 2. Do the workflow by hand first (29:04)

> "Whatever workflow you're setting up, run it end to end. Step into each node yourself. Run the pass, run the condition, watch the functions get executed, do the review, do the ship to production — and *then* start writing this all as a combination of agents, engineers, and code."

He recommends sketching in Mermaid first. (The animated diagram in the video was itself one-shotted by a plan-build-test ADW — 29:45.)

### 3. Agents *and* code — not agents alone (30:07)

> "This is not just about token cost. This is about performance, reliability, and speed. Speed costs zero tokens. There's no hallucination. It does the exact same thing every time. And it literally runs at the speed of light." — 30:21
>
> "Yes, during the process, you'll wonder, 'I should just throw this all in the skill.' You'll be wrong down the road. I can guarantee you that. I've been there." — 30:55

Classic engineering discipline matters *more*, not less: "isolatable, decoupled, single interface... because once you do it right, it gets multiplied hundreds and thousands of times." (31:41)

## What to discount

- **1:25–3:33** is a credibility segment positioning him against Boris Cherny (Anthropic) and Peter Steinberger (OpenAI). Skippable.
- **32:10–end** is a sustained pitch for his paid course (agenticengineer.com / Tactical Agentic Coding, 8 lessons + 6 upgradeable, 30-day refund before lesson 4). The free equivalent is his "Thinking in Threads" blog post.
- The video is **entirely conceptual** — no code, no repo, no SDK, no concrete implementation. The diagram is the deliverable. Treat it as an architecture argument, not a tutorial.

---

# Part 2 — Thread-Based Engineering (the measurement)

Video 1 answers *what to build*. This one answers *how do you know you're getting better at running it* — the question the factory's success metrics have to come from.

## The hook

Karpathy: *"I've never felt this much behind as a programmer."* Paired with Boris Cherny — creator of Claude Code — publicly posting his personal setup. Dan's read: the gap between engineers using agents well and everyone else is widening, it's a skill issue, and a new skill needs a framework to measure against.

> "If you don't measure it, you will not be able to improve it." — 1:47

## The primitive

A **thread** is a unit of engineering work over time, driven by you and your agents. Two mandatory nodes, one middle:

```
[P] Prompt / Plan ──── Agent Work (tool calls) ──── [R] Review / Validate
```

Same two human bookends as the ADW video (7:09 there). The reframe is the middle:

> "Tool calls roughly equal impact, assuming you're prompting something useful. Pre-2023, you and I *were* the tool calls. We were updating the code, we were reading, we were doing the web requests." — 3:28

**Every metric in the framework reduces to total tool calls executed on your behalf.** That's the unit.

## The six threads

| | Thread | Shape | Use when |
|---|---|---|---|
| 1 | **Base** | `P → work → R` | The atom. One prompt, one line of work. |
| 2 | **P-thread** (parallel) | N base threads at once | Scale through parallelism — terminals, worktrees, sandboxes |
| 3 | **C-thread** (chained) | `P→R → P→R → P→R` | Work exceeding one context window, or high-risk production |
| 4 | **F-thread** (fusion) | N parallel → **fuse** into one | Best-of-N, cherry-picking, confidence through agreement |
| 5 | **B-thread** (big / meta) | A thread *containing* threads | Prompts firing prompts — subagents, orchestrators, ADWs |
| 6 | **L-thread** (long) | Base thread, hundreds of steps | High autonomy, hours-to-days, no intervention |

### P-thread — parallel (4:07)

Boris runs 5 Claude Codes in numbered terminal tabs, plus 5–10 more in the Claude Code web interface kicked off with `@`, "teleporting back and forth." Dan's version is a `pthread` alias plus a fork-terminal skill that spawns N instances against one prompt.

Self-check, and a good one:

> "If you have to sit and babysit a single agent, you probably need to scale down your threads and just work on a single thread of work." — 7:53

### C-thread — chained (9:01)

Explicitly **not** for recovering from agent mistakes — *"that's bad agent coding."* It's for *intentionally* chunking work: either it won't fit one context window, or it's high-risk production (a migration where one wrong step crashes prod).

Mechanisms: Claude Code's AskUserQuestion tool, system notifications on phase completion, and a text-to-speech hook that has the agent narrate what it finished. He flags this as the first thread to be suspicious of:

> "Do you need to break this work down into phases? If you do, C-threads are great. If you don't, just use a base thread." — 11:16

### F-thread — fusion (12:22) — his favorite

Same (or similar) prompt to N agents, then **aggregate the results**. Demo at 13:00: nine agents in parallel — 3× Claude Code, 3× Gemini, 3× Codex — each in its own sandbox, then fused.

Two distinct uses, and the second is the underrated one:

- **Rapid prototyping** — best-of-N, or cherry-pick ideas across several results
- **Confidence** — *"If you ask one agent a question, it'll say something back. If you ask five, you'll get much higher confidence. Say four out of five gave you the exact same answer."*

> "The future of rapid prototyping will be done with fusion threads. You can mark my words on that. I'm betting big on this trend." — 15:13

**The distinction from a P-thread is the fuse step.** A P-thread scales output; an F-thread scales *confidence* by consolidating. This distinction is missing from the ADW video and it changes our Phase 4.

### B-thread — big / meta (15:25)

A thread containing threads. From your seat it's still one prompt and one review; the internals are a black box *because you engineered them*. Subagents are the simplest example; orchestrator agents (an agent kicking off plan → scout → build → review → staging agents) are the next tier.

This is where he ties in **Ralph Wiggum** (18:04) — a loop over an agent, credited to Geoff Huntley:

> "AI engineers are starting to figure out that agents plus code outperforms agents alone. [...] We know this as AI developer workflows, ADWs, but it is great to see a pattern like this really hitting the mainstream."

**The B-thread is the software factory from Part 1.** Same object, different vocabulary.

### L-thread — long (19:06)

High autonomy, long duration, no human intervention. Hundreds to thousands of tool calls, running for hours. Boris posted a run at **1 day, 2 hours**.

Note the shape: it comes full circle to the base thread — *"same shape, just longer, more tool calls."* Nothing clever; better prompting, better models, better context management, better tools. He points back at the **core four: context, model, prompt, tools.**

## The mechanism: Claude Code's stop hook (21:20)

This is the most implementation-relevant thing in either video, and it's the Claude-Code-native version of Part 1's agents-plus-code separation:

```
agent tries to stop
  → stop hook fires
    → YOUR deterministic code runs
      → check a progress file / run a validation command
        → re-loop  OR  complete
```

> "The stop hook can intercept, it can run some code, it can check a progress file, it can run a validation command, and then it can continue the workflow where you re-loop over again, or it completes the work. [...] It allows you to tap into that deterministic traditional code plus agents."

This is precisely the 27:15 requirement from Part 1 — code gates the agent, not a skill step inside the agent — expressed as a Claude Code primitive. **It belongs in Phase 1.**

## The hidden seventh: the Z-thread (26:34)

Zero touch. The review node is deleted entirely. Maximum trust.

> "It isn't that we don't look at the code — it's that we know we don't have to. That's the endgame."

He withholds the detail (course material) and pre-empts the objection: *"I don't want to confuse newer engineers into thinking this is vibe coding."* This is the same claim as "the best teams drop engineering review" from Part 1 (21:10), and we reject it for the same reason — see Divergences.

## Four ways to improve (23:52)

The framework's actual deliverable:

| Axis | Means |
|---|---|
| **More** threads | P-threads — parallel execution paths |
| **Longer** threads | L-threads — extended duration without intervention |
| **Thicker** threads | B-threads — more work per unit time, nested underneath |
| **Fewer** checkpoints | Reduce human-in-the-loop steps; increase trust |

## Boris Cherny's actual setup

From the tweet screenshots, since it's the only concrete practitioner data in either video:

- 5 Claude Codes in terminal (tabs numbered 1–5); 5–10 more in Claude Code web
- **Opus 4.5 with thinking, always** — *"even though it's bigger & slower than Sonnet, since you have to steer it less and it's better at tool use, it's almost always faster than using a smaller model in the end"*
- One shared `CLAUDE.md` checked into git; whole team contributes multiple times a week; when Claude does something wrong they add it. **Kept deliberately small.**
- **Does not** use `--dangerously-skip-permissions` — configures specific permissions instead
- System notification when Claude Code needs input; stop hook for long-running tasks
- Top tip: **give Claude a way to verify its own work**

That last one is the cheap version of keeping human review: a validation command each lane must pass before anything reaches us.

## How the two videos map

| Thread (video 2) | ADW node (video 1) | Our phase |
|---|---|---|
| Base | The 4:52 atom | Phase 1 |
| P-thread | 9:35 worktree fan-out | Phase 4 — **and what `pravado-i0/i1/i2` already was, by hand** |
| C-thread | *(not covered)* | Migrations / Supabase work |
| F-thread | *(not covered)* | Phase 4 — fuse, don't only pick |
| B-thread | The software factory itself | Phase 5 |
| L-thread | What the factory enables | Phase 3 nightly sweep |
| Z-thread | "Best teams drop review" (21:10) | **Rejected** — see Divergences |

---

# Part 3 — Applying it to Saipien Labs

## Where we actually are

A survey of `~/projects` on 2026-07-29 — 41 directories:

```
ACTIVE (last commit 2026)
  pravado-v2                        2026-07-22   node supabase CLAUDE.md .claude/ gha
  datum-zero                        2026-07-15   node CLAUDE.md .claude/ gha
  pravado-i0 / i1 / i2              2026-07-03   node CLAUDE.md .claude/ gha   ← same day
  pravado-v2-f13-fix                2026-07-02   node CLAUDE.md .claude/ gha
  pravado-v2-login-magic            2026-06-30   node CLAUDE.md .claude/ gha
  SLATE-OS                          2026-06-18   node next supabase .claude/
  pravado-v2-01 … -06d, -0b, -0c    2026-06-02 → 06-10   (10 dirs)   ← one week
  sapient-digital                   2026-05-19   node next .claude/
  wellstead-platform                2026-05-08   node next supabase .claude/ gha
  pravado-v2-backup-2026-04-24      2026-04-24
  claudetube                        2026-02-05   py CLAUDE.md .claude/ gha
  wellstead-platform-{admin, automations, customer-app, database,
    loyalty, partner-portal}        2026-02-05   ← all seven, same day
  wellstead-customer-app            2026-02-16
  aivery-platform                   2026-02-03

DORMANT (2025 or no commits)
  pravado-platform, pravado-platform-saas, pravado-marketing-site,
  pravado-main-site, pravado-campaign, pravado-v2-foundation,
  insightforge-pulse, iron-and-honor, saipien-labs, media-database,
  wellstead-partner-app, agents (empty)
```

### The finding

**We have already run Dan's step 2 — roughly twenty times.**

`pravado-i0` / `i1` / `i2`, created the same day, is the **9:35 fan-out node** executed by hand: N parallel attempts at the same problem, keep the winner. The ten `pravado-v2-0*` directories in one week is the same pattern at larger scale. The seven `wellstead-platform-*` directories on 2026-02-05 is a scaffold-out ADW, done by hand.

> Inference, not certainty: these could be sequential retries rather than parallel attempts. Either reading gives the same conclusion — **whole-repo copies are standing in for a workflow.**

We're not at the beginning of Dan's advice. We're at the end of it. The 41 directories are what it costs to run the loop manually. The factory is the automation of a process we've already executed by hand enough times to know its shape.

### The unfair advantage

Nearly every live repo is the same stack: **Node + Next + Supabase + `.claude/` + GitHub Actions**. That homogeneity is unusual and it is the thing that makes a portable factory cheap here. One manifest schema, one set of ADWs, works across the portfolio on day one. Most orgs pay for portability in per-stack adapters. We don't.

## The reframe: the model inverts for a solo studio

Dan designed for an org. Two of his nodes are org-shaped (kanban intake from Support/Product, engineer review as a *second* person). Our constraint profile is the mirror image:

| | His factory | Ours |
|---|---|---|
| Serves | 1 codebase, many people | 1 person, many codebases |
| Intake problem | Translating others' tickets | Doesn't exist — we are the router |
| Scarce resource | Team coordination | **Review bandwidth**, full stop |
| Binding constraint | Org alignment | **Portability across N repos** |
| Compute budget | Company | Personal burn |
| The durable asset | The product | **The factory** |

That last row is the strategy. For a venture studio the factory is not infrastructure *for* a product — it is the studio's compounding asset. Ventures churn; the factory carries over. Venture #7 should cost a fraction of venture #1 **because** the factory exists. The video does not cover this, because he isn't running a studio.

### The honest ROI boundary

A factory pays off on **repetition**. In a venture studio, repetition lives in:

- ✅ Portfolio maintenance across 40 repos (patches, deps, drift, dead-repo detection)
- ✅ New-venture bootstrap
- ✅ Deploy / CI lanes, identical across a homogeneous stack
- ✅ Post-validation feature work on a product with users

It does **not** live in pre-PMF feature work on an unvalidated product. Building a factory around a venture that dies in six weeks is building the system that builds nothing. Justify every phase below against the *portfolio*, not against any single venture.

---

# Part 4 — The build plan

## Architecture

### The kernel

`~/agents` is empty and a year stale. Claim it. It holds every ADW; **nothing is copied into ventures.**

```
~/agents/
  adw/
    lanes/
      chore.ts          # Phase 1 — build agent + code gates
      sweep.ts          # Phase 3 — portfolio-wide
      fanout.ts         # Phase 4 — N worktrees, keep winner
      feature.ts        # Phase 5 — plan → build → test → review
      bootstrap.ts      # Phase 6 — new venture
    nodes/
      gates.ts          # lint / typecheck / test / build — PURE CODE, no agent
      worktree.ts       # create / merge / destroy — PURE CODE
      manifest.ts       # load + validate .adw.yml
      state.ts          # run-state store
    agents/
      scout.md  plan.md  build.md  review.md   # system prompts, frozen
  runs/
    <run-id>/
      manifest.json  plan.md  diff.patch  gates.json  review.md
  registry.yml          # which repos are in the portfolio, and their lane opt-ins
```

**The `nodes/` vs `agents/` split is the whole point of 27:15.** Gates are code. They run, they exit non-zero, and their stderr is handed back to the build agent's *same session*. They are never a step inside a skill.

### The per-venture surface

One file per repo. This is the abstraction that turns "an ADW" into "a factory":

```yaml
# .adw.yml
stack: next-supabase
install:   pnpm install --frozen-lockfile
lint:      pnpm lint
typecheck: pnpm tsc --noEmit
test:      pnpm test
build:     pnpm build
deploy:                                    # empty = no auto-deploy lane
danger:    [supabase/migrations/**, .env*] # never auto-edit
lanes:     [chore, sweep]                  # opt-in per repo
```

### The state store

Dan's "information orchestration" (30:44). Every node writes its output to `runs/<id>/` as a file; the next node reads from disk. Context moves through the filesystem, not through one long agent conversation. This is what makes each node independently testable — his stated reason for the whole separation (31:25).

## Phases

### Phase 0 — Scaffold the kernel
- [ ] Claim `~/agents`, lay out the tree above
- [ ] Write `.adw.yml` schema + validator
- [ ] Write the run-state store
- [ ] **Deliverable:** empty kernel that can load a manifest and open a run directory

### Phase 1 — The chore lane, one repo
A **base thread** with code gates. Target: `datum-zero` (recent, has CLAUDE.md + GHA, no Supabase to break).

- [ ] Build agent runs → **code** runs lint/typecheck/test → failures feed back to the same session ID
- [ ] **Implement via the Claude Code stop hook** (Part 2, 21:20): agent tries to stop → hook fires → our code checks the run-state progress file and runs the validation command → re-loop or complete. This is the native mechanism for the 27:15 separation; don't hand-roll a loop around it.
- [ ] Every lane declares a **verification command** it must pass before anything reaches us (Boris's top tip). `.adw.yml` already has the fields.
- [ ] Hard stop after N retries; write everything to `runs/`
- [ ] Human review at the end, always
- [ ] **Deliverable:** Dan's 27:04 diagram, correctly separated
- [ ] **Test:** intentionally break a type, confirm the loop catches and fixes it

### Phase 2 — Portability proof
- [ ] Extract everything repo-specific into `.adw.yml`
- [ ] Run the *identical* lane on `sapient-digital`
- [ ] **Do not skip this.** If the kernel needs code changes for repo #2, the abstraction is wrong and it's cheap to fix now

### Phase 3 — The portfolio sweep ⭐ **primary ROI**
41 directories, one engineer. This is the thing we cannot do without a factory.

- [ ] `registry.yml` — classify all 41 dirs: active / dormant / dead
- [ ] Nightly sweep: dependency bumps, security patches, lint/type drift
- [ ] Dead-repo report: what hasn't moved in 90 days, what has no remote, what duplicates what
- [ ] **Deliverable:** a morning report we couldn't otherwise produce
- [ ] Everything before this phase is setup. This is where the factory earns its keep.

### Phase 4 — The fan-out lane → build it as an **F-thread**, not a P-thread
Automate `pravado-i0/i1/i2`.

The original plan said "keep the winner." Part 2 argues that's leaving value on the table: a P-thread scales *output*, an F-thread scales *confidence* by consolidating. Same compute either way — the fuse step is nearly free and strictly better.

- [ ] One prompt → N git worktrees → parallel attempt → gates
- [ ] **Fuse, don't just pick.** Two modes, both worth having:
  - **Best-of-N + cherry-pick** for prototyping — take the winner, graft the good ideas from runners-up
  - **Agreement as confidence** for review/audit — if 4 of 5 agents flag the same thing, that finding is real; if 1 of 5 does, it probably isn't. This is the cheapest quality lever in the whole plan.
- [ ] Losers destroyed automatically. **This deletes the sprawl at the source.**
- [ ] Cap N at 3 (see divergences below)
- [ ] Cross-model fusion (Claude + others) is optional and later — single-model N=3 captures most of the confidence gain

### Phase 5 — Router + feature lane
- [ ] Intake (GitHub issues or a flat file — we don't need Jira)
- [ ] Classify chore / bug / feature — cheap single-shot, batchable
- [ ] Feature lane: scout → plan → build → test → **human review** → ship
- [ ] Only after 1–4 are load-bearing

### Phase 6 — Venture bootstrap ADW
Studio-specific; absent from the video. `wellstead-platform-*` on 2026-02-05 proves the demand.

- [ ] "New venture" → Next + Supabase + `.claude/` + GHA + `.adw.yml` + deploy skeleton
- [ ] This is what makes venture #7 cheap, and it's the clearest expression of the studio thesis

## Success metrics

The factory needs a scoreboard or we won't know it's working. Part 2's four axes are it — all four reduce to **total tool calls the factory executes on our behalf**, which is the number to trend.

| Axis | Metric | Where it comes from | Target |
|---|---|---|---|
| **More** | Concurrent threads at peak | Phase 4 fan-out, parallel lanes | 1 → 3–5 |
| **Longer** | Longest unattended run | Phase 3 sweep, Phase 5 feature lane | minutes → hours |
| **Thicker** | Sub-threads per top-level prompt | Phase 5 orchestration | 1 → 3+ |
| **Fewer** | Human checkpoints per shipped change | Every lane | trending down, **never to zero** |

Instrument this from Phase 1 — `runs/<id>/` should record tool-call count, wall-clock duration, sub-thread count, and human-touch count. It's four fields and it's the only evidence we'll have that the factory is compounding rather than just existing.

Two cautions on reading the scoreboard:

- **These are diagnostics, not targets.** Optimizing tool-call count directly produces agents that thrash. The number is a proxy for delegated work; treat a drop as a question, not a failure.
- **"Fewer checkpoints" has a floor for us.** See Divergences.

## Model and cost mechanics

| Node | Model | Effort | Rationale |
|---|---|---|---|
| Scout / plan | `claude-opus-5` | `high`–`xhigh` | Nothing missed upstream |
| Build | `claude-opus-5` | `xhigh` → sweep down | $5/$25 per MTok, 1M context; strongest on multi-file work |
| Chore lane | `claude-sonnet-5` | `low`–`medium` | $3/$15, intro $2/$10 through 2026-08-31 |
| Triage / classify | `claude-haiku-4-5` | — | $1/$5, single-shot |

Four factory-specific concerns:

1. **Sweep effort downward per lane.** Opus 5's `low` and `medium` are unusually strong — often beating prior models at `xhigh`. Start at `xhigh` for coding, then step down against real evals. Defaults carried from older models don't transfer. **This is the biggest cost lever.**

2. **Freeze the kernel's system prompts and tool list.** Prompt caching is a prefix match — any byte change invalidates everything after it. Opus 5's cache minimum is 512 tokens. A factory running the same ADW hundreds of times lives or dies on hit rate; a `Date.now()` in a system prompt silently costs ~10x per run. Verify with `usage.cache_read_input_tokens` — if it's zero across repeated runs, something is invalidating the prefix.

3. **Cap subagent spawning explicitly.** Opus 5 delegates *more* readily than 4.8 — the opposite direction from prior guidance — and each subagent re-establishes context. Put a hard ceiling in the kernel prompt.

4. **Two prompt inversions for Opus 5:**
   - **Delete verification scaffolding.** It verifies its own work unprompted; "double-check your answer" now causes over-verification with no quality gain.
   - **Never write "only report high-severity issues"** in the review lane. Opus 5 follows severity filters literally and measured recall drops. Have it report everything with confidence + severity, then filter in a separate cheap pass.

### Surfaces

- **Claude Agent SDK** (`@anthropic-ai/claude-agent-sdk`) — the harness for the kernel. Claude Code as a library: built-in tools, sessions (needed for the same-session-ID feedback of 27:15), subagents, hooks, permissions. Separate product from the API SDK; docs at `code.claude.com/docs/en/agent-sdk`.
- **Batches API** — 50% off, fits the Phase 5 triage/classify node (single-shot, not agent loops).
- **Managed Agents scheduled deployments** — cron-scheduled agent sessions in an Anthropic-hosted sandbox. That's the Phase 3 nightly sweep with no server and no laptop staying awake. Beta. Start local; move the sweep there once it's stable.

## Where we diverge from the videos

**Keep human review — reject the Z-thread.** He makes this claim twice: "the best teams drop engineering review" (v1, 21:10) and the Z-thread as endgame (v2, 26:34). Both assume an org with redundancy. We have no second engineer, so review *is* the quality bar and removing it means nothing catches a bad merge.

The right move is to make review **cheap**, not absent — and Part 2 hands us three concrete ways:

- **Verification commands per lane** (Boris's top tip) — the agent proves its own work before we look
- **F-thread agreement as a pre-filter** — 4-of-5 consensus surfaces what's real, so we review findings rather than raw diffs
- **Small diffs + forced summaries** — reduce the cost per review rather than the count

"Fewer checkpoints" is a legitimate axis to push on; **zero is not our target.** Push the floor down, don't delete it.

**Cap racing at 2–3, not 5–10.** At 16:50 he races many sandboxes for a hotfix. That's an org compute budget against an org-sized incident cost. Ours is personal burn.

**"Never touch the product" is wrong pre-PMF.** At 21:54 he says the best teams don't touch the app layer. True for a scaled product with users. For an unvalidated venture, touching the product *is* the job. Our factory's justification is the portfolio, not any single venture's features.

**Worktrees are fine much longer than he suggests.** He moves off them at 10:32 in favor of per-agent sandboxes. Sandboxes cost money and setup. Worktrees on one machine will carry us past Phase 4 — and would already have prevented most of the 41 directories.

**We get one thing he doesn't have:** no ticket-translation problem, no org politics, and a same-day feedback loop on the factory itself. When we notice a flaw in an ADW we can fix it that afternoon. His 24:00 rant about product managers writing untranslatable tickets simply doesn't apply.

## Open questions

1. **Are the `pravado-v2-*` and `pravado-i*` directories dead?** Fourteen Pravado-lineage directories exist. If they're dead experiments, Phase 3's first useful act is a dry-run report flagging abandoned repos. If any are load-bearing, that changes the registry.
2. **Is `wellstead-platform-*` a deliberate 7-service split, or an abandoned decomposition?** All seven landed 2026-02-05 and haven't moved since; the monolithic `wellstead-platform` moved later (2026-05-08).
3. **Which ventures are post-validation?** Determines which get the feature lane vs. sweep-only.
4. **Deploy targets per repo** — needed before the `deploy:` field in `.adw.yml` means anything.

---

## Immediate next action

Phase 0 + Phase 1 against `datum-zero`, then Phase 2 against `sapient-digital` to prove portability. That's the smallest increment that produces a real, reusable factory component rather than a one-off script.

In parallel — and arguably higher immediate value — a **Phase 3 dry run**: classify all 41 directories and produce the dead-repo report. It's cheap, it answers open questions 1 and 2, and it delivers something we don't currently have.
