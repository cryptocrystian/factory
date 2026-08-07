# Super Simple Software Factory — Implementation Guide

**Video:** [My Super Simple Software Factory (For Agentic Engineers)](https://youtu.be/haUfb1ievTE) — IndyDevDan, 29:52
**Repo:** https://github.com/disler/super-simple-software-factory (MIT)
**Captured:** 2026-08-03 · **Revision 3** — verified against the cloned source; no inference remains.

> **Status of this document.** Revisions 1–2 were reconstructed from video frames. Revision 3 is
> checked against the actual repository. Where the video and the repo differ, the repo wins and
> the difference is noted. Two claims from earlier revisions were **wrong** and are corrected
> below (§7.3 on tool allowlists, §3 on repo layout).

---

## 1. What you are actually building

A **software factory** gives you more leverage on a single prompt. You type one request; a
deterministic Python script expands it into a software development lifecycle executed by coding
agents inside bounded phases, with code gates between them.

The organizing constraint:

> **Agent proposes, code disposes.**

Three actors, and every phase declares which one owns it:

| Actor | `kind=` | Role |
|---|---|---|
| **Engineer** | `engineer` | Supplies intent (the request) |
| **Agent** | `agent` | Plans, builds, reviews, documents — the parts needing reading and deciding |
| **Code** | `code` | Sequences, validates, gates, commits — anything you can write the invocation down for |

Success criterion: *does the thousandth run look like the first?* Every decision follows from that.

---

## 2. Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — ADWs are PEP-723 scripts (`#!/usr/bin/env -S uv run` with
  inline deps: `pydantic`, `python-dotenv`, `pyyaml`, `rich`). No venv to manage.
- **[Pi coding agent](https://github.com/disler)** on PATH (`pi --version`). v1 is Pi-only;
  `coding_agent: claude_code` is schema-valid but `agent_cc.py` raises until v2.
- **`OPENROUTER_API_KEY`** in `.env`. That's the only key v1 needs.
- **A git repo with at least one commit** — commit phases call `git_helper.commit_all`, which
  raises outside a repo.
- **Optional:** `just` (convenience wrapper only — every recipe is one line), `bun` (only for the
  visualizer UI), `sqlite3` (for reading traces without the UI).

---

## 3. What the repo actually is **[CORRECTED]**

Revision 1 assumed the repo contained a working `adws/` tree. It does not. The distributed repo
is **the skill and nothing else** — the factory is *stamped out of it* into your codebase.

```
super-simple-software-factory/
└── .claude/skills/sssf/
    ├── SKILL.md                        # hard rules + request routing
    ├── cookbooks/                      # 9 orchestrator playbooks, lazily loaded
    ├── references/                     # config / handoff / observability specs
    ├── scripts/                        # install.py, make_config.py, make_adw.py
    ├── apps/visualizer/                # read-only trace UI (Vue + Vite on Bun)
    └── templates/                      # EXACTLY what install.py stamps
        ├── sssf.config.yaml
        ├── env.sample
        ├── justfile
        ├── prompt_engineering/{agent}/ # system.md + user.md
        ├── harness_engineering/        # subagents.ts, themeMap.ts
        └── adws/
            ├── adw_*.py                # the twelve starter workflows
            └── adw_modules/            # ALL low-level logic
```

The repo's `example` branch has the factory already stamped in, with a demo app the factory
planned/built/tested/reviewed/documented and the traces from those runs. **Start there** if you
want to see it populated rather than as templates.

### 3.1 After `install.py`, your repo looks like

```
your-repo/
├── adws/
│   ├── adw_sssf_config/sssf.config.yaml    # the roster — yours to edit
│   ├── adw_modules/                        # all low-level logic
│   ├── adw_data/
│   │   ├── prompt_engineering/{agent}/     # system.md + user.md — yours
│   │   ├── harness_engineering/            # pi extensions — yours
│   │   ├── sessions/{adw_id}/              # runtime, gitignored
│   │   └── sssf.db                         # SQLite trace, gitignored
│   └── adw_*.py                            # twelve workflows
├── justfile
└── .env                                    # OPENROUTER_API_KEY
```

`install.py` is **idempotent** — it skips every file that already exists and reports what it
skipped, so a re-run doubles as a drift check. `--force` overwrites **everything**, including
your config and prompts. Commit before you force.

### 3.2 The three layers

```
┌─────────────────────────────────────────────────────────┐
│  OPERATOR LAYER   — SKILL.md; an agent reads it to drive │
├─────────────────────────────────────────────────────────┤
│  WORKFLOW LAYER   — adw_*.py. Owns sequencing, retries,  │
│                     gates, commits, acceptance           │
├─────────────────────────────────────────────────────────┤
│  AGENT LAYER      — Pi sessions, bounded per phase       │
└─────────────────────────────────────────────────────────┘
                          ↕
   files (raw record) + SQLite (queryable mirror) → polled UI
```

**The inversion that matters:** code controls the workflow; agents are called *by* it. The
opposite of "hand the agent tools and let it sequence."

---

## 4. The ten hard rules **[SHOWN — SKILL.md]**

Enforced across everything the factory generates. This is the spec; everything else is detail.

1. **Validate before running** — every ADW declares `REQUIRED_AGENTS` and calls
   `agents.validate()` first; a missing or misnamed agent fails before anything spawns.
2. **Typed outputs only** — every agent call pairs with a concrete `EnvelopeBase` subclass. Parse
   failures re-prompt the *same* session (context intact), never restart.
   **The output contract is a synced triad:** (a) the type in `data_types.py`, (b) the JSON
   example in the agent's `user.md` `## Report` section, (c) `output_type=` at every call site.
   Change one, change all three in the same edit.
3. **Gates validate claims, not guesses** — `gate(envelope, run) -> GateReport`; failures return
   to the same session as corrections.
4. **Four-param rule** — any function with more than 4 parameters takes one concrete data type
   instead. `AgentCall` and `PhaseParams` are the pattern.
5. **One agent, one prompt, one purpose** — identity lives in `system.md`; task shape (user prompt
   + output type) lives at the call site.
6. **ADW scripts stay thin** — all low-level logic lives in `adw_modules/`.
7. **Every phase earns a description** — one sentence on what it does and *why*, never a
   restatement of the name. `commit_plan: "Commit the plan"` is **rejected at construction**, and
   so is blank. It is the only intent the trace, console, and UI ever show.
8. **A known command is code, not an agent** — if you can write the invocation down (`bun test`,
   `ruff check`), it belongs in a `kind="code"` phase via `quality.py`. Agents are for parts that
   need reading and deciding. Failures come back to the builder as an envelope either way.
9. **`tools:` is a capability list, `writes:` is the boundary** — see §7.3. Enforced in
   `permissions.py` after every agent call; unauthorized changes are rolled back and the phase dies.
10. **Every ADW ends in `run.finish(accepted=…)`** — phases passing is not the run being accepted.
    A test phase that ran a red suite *succeeded at its job*. Pass `accepted=` so exit code,
    session status, and banner are decided together and cannot disagree.

---

## 5. The twelve starter workflows **[SHOWN]**

Each ADW's `Phases:` docstring line is the contract. `[...]` marks bounded loops.

| ADW | Chain |
|---|---|
| `adw_prompt` | engineer(request) → \<agent\> |
| `adw_plan` | engineer(request) → planner |
| `adw_build` | engineer(request) → builder |
| `adw_scout` | engineer(request) → scout |
| `adw_quality` | engineer(request) → code(quality) |
| `adw_document` | engineer(request) → code(changes) → documenter |
| `adw_plan_build` | engineer → planner → builder → git(commit) |
| `adw_build_test` | engineer → builder → code(test) [→ builder(fix) → code(test) … bounded] |
| `adw_build_review` | engineer → builder → reviewer [→ builder(revise) → reviewer … bounded] |
| `adw_plan_build_test` | engineer → planner → builder → code(test) [→ builder(fix) …] → git(commit) |
| `adw_plan_build_test_quality` | engineer → planner → builder → [code(verify) → code(test) → builder(fix)] bounded → git(commit) |
| `adw_simple_sdlc` | the full chain — §6 |

Invocation is uniform:

```bash
uv run adws/adw_simple_sdlc.py "<prompt or path/to/prompt.md>" \
  [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]
```

`just` recipes wrap these: `just demo`, `just prompt`, `just scout`, `just plan`, `just plan-build`,
`just sdlc`, plus trace reads `just sessions`, `just phases <id>`, `just tail <id>`,
`just procs <id>`, `just obs`.

---

## 6. `adw_simple_sdlc.py` — the reference implementation **[SHOWN, verbatim]**

183 lines. `MAX_FIX_LOOPS = 3`, `MAX_REVISION_LOOPS = 2`,
`REQUIRED_AGENTS = ["planner", "builder", "reviewer", "documenter"]`.

**Three commits, three work products, three authors** — the plan, the code, and the write-up each
land in their own commit, each message written by the agent that produced it. No agent's sentence
is ever reused for another agent's diff.

### 6.1 Setup

```python
def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml",
         adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)          # rule 1
    run = session.ensure(cfg, adw_id)
    baseline = git_helper.rev("HEAD")              # pinned before this run commits anything

    def commit(ph, envelope) -> None:
        """Commit what the preceding phase produced, in that agent's own words."""
        message = envelope.commit_message or f"sssf({run.adw_id}): {envelope.summary}"
        ph.log(sha=git_helper.commit_all(message), message=message)

    def record(ph, result) -> None:
        """Log a deterministic block's verdict — the same shape every ADW uses."""
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))
```

### 6.2 Plan → commit → build

```python
    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                   description="Capture the incoming ask")) as ph:
        ph.log(input=prompt, baseline=git_helper.short_sha(baseline))

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                   description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                       gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="commit_plan", kind="code", owner="git",
                   description="Put the spec on record before any code exists to blur it")) as ph:
        commit(ph, plan)

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                   description="Implement the plan exactly")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                        gates=[gates.diff_matches_claims]))
```

### 6.3 Test/fix loop (bounded, 3)

```python
    test = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                       description="Run the suite — a known command, so code runs "
                                   "it and no agent has to rediscover it")) as ph:
            test = quality.run_tests(run)
            record(ph, test)

        if test.passed:
            break

        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                       description="Repair what the suite reported, from its "
                                   "verbatim output")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                            previous=quality.as_envelope(test, "tests"),
                            gates=[gates.diff_matches_claims]))
```

From the module docstring: *"`bun test` is a command, not a judgement call: an agent rediscovering
it every run costs a million tokens to learn what a subprocess already knows."*

### 6.4 Review/revise loop (bounded, 2)

```python
    review = None
    revised = False
    for i in range(1, MAX_REVISION_LOOPS + 1):
        with run.phase(PhaseParams(name=f"review_{i}", kind="agent", owner="reviewer",
                       description="Confirm the build matches the plan")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prompt, previous=build,
                             gates=[gates.artifacts_exist, gates.verdict_consistent]))

        if review.approved or i == MAX_REVISION_LOOPS:
            break

        with run.phase(PhaseParams(name=f"revise_{i}", kind="agent", owner="builder", retries=1,
                       description="Close the reviewer's blocking findings")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=review,
                            gates=[gates.diff_matches_claims]))
            revised = True
```

**Two different questions, asked in order.** The suite asks *does it run*; the reviewer asks *is
this what was asked for*, against `plan.md`. Neither can answer the other's.

### 6.5 The stale-green-light guard — the single best idea to steal

```python
    # A revision edited code after the suite last ran, so the green light is
    # stale. Re-run it rather than commit on a result that predates the change.
    if revised and review is not None and review.approved:
        with run.phase(PhaseParams(name="retest", kind="code", owner="quality",
                       description="Re-run the suite — the revision changed code "
                                   "after the last green result")) as ph:
            test = quality.run_tests(run)
            record(ph, test)
```

Tests pass → reviewer demands changes → builder revises → **the passing result is now stale**.
The tree that gets committed is the tree that was both tested *and* approved.

### 6.6 Verification gate and failure semantics

```python
    # Red tests or a rejected review stop the chain here: the code stays
    # uncommitted and nothing is documented, because there is nothing worth
    # describing yet. The plan commit stands — it is a record of what was asked.
    verified = (test is not None and test.passed
                and review is not None and review.approved)
    if verified:
        with run.phase(PhaseParams(name="commit_build", kind="code", owner="git",
                       description="Land the code only now: green suite, approved review")) as ph:
            commit(ph, build)

        with run.phase(PhaseParams(name="changes", kind="code", owner="git",
                       description="Diff the whole run against its pinned baseline, "
                                   "for the documenter")) as ph:
            changeset = changes.capture(run, ChangeCapture(base=baseline))
            ph.log(base=f"{changeset.base.label} @ {changeset.base.commit[:7]}",
                   reason=changeset.base.reason,
                   files=len(changeset.files) + len(changeset.untracked),
                   lines=f"+{changeset.insertions} -{changeset.deletions}",
                   diff=changeset.diff_path)
            if changeset.empty:
                raise RuntimeError(
                    f"nothing changed since {changeset.base.label} "
                    f"({changeset.base.reason}) — there is nothing to document.")

        with run.phase(PhaseParams(name="document", kind="agent", owner="documenter", retries=1,
                       description="Write up the completed change")) as ph:
            document = ph.call(AgentCall(output_type=DocumentOutput, prompt=prompt,
                               previous=changes.as_envelope(changeset, DOCUMENT_NOTES),
                               gates=[gates.artifacts_exist, gates.files_non_empty]))

        with run.phase(PhaseParams(name="commit_docs", kind="code", owner="git",
                       description="Ship the write-up in its own commit, "
                                   "beside the code it describes")) as ph:
            commit(ph, document)

    return run.finish(accepted=verified,
                      reason="the suite or the review never came back clean")
```

Four rules encoded:

1. **The code commit lands after verification, not after the build.** Fixes and revisions are part
   of the same work product; red code has no business on the branch.
2. **Failure leaves the plan committed and the tree dirty.** The spec is a real artifact either
   way, and unfinished code stays where the engineer can see it.
3. **Empty changeset raises.** A run that changed nothing but claims success is a bug.
4. **`run.finish(accepted=)` decides everything at once** — exit code, session status, banner.

The documenter measures against the commit this run *started* from, not `main` — by then the run
has moved `main` itself.

---

## 7. Configuration — `sssf.config.yaml` **[SHOWN, complete]**

### 7.1 Defaults

```yaml
defaults:
  coding_agent: pi
  model: google/gemini-3.6-flash   # provider/id — a bare pattern is ambiguous across providers
  thinking: medium                 # off | minimal | low | medium | high | xhigh | max
  harness_engineering: []          # pi extensions loaded into the harness (-e)
  tools:                           # roster-wide allowlist; any agent may override
    - read      # read file contents
    - bash      # execute bash commands
    - edit      # find/replace edits
    - write     # create/overwrite files
    - grep      # search file contents  (pi default: OFF)
    - find      # find files by glob     (pi default: OFF)
    - ls        # list directories       (pi default: OFF)
  protected_files:                 # off-limits unless an agent names them in its own `writes`
    - adws/adw_modules/
    - adws/adw_sssf_config/
    - adws/adw_*.py
  data_dir: adws/adw_data          # runtime: {data_dir}/sessions/{adw_id}/{agent_name}/

observability:
  db: adws/adw_data/sssf.db
  poll_ms: 500
```

Two footguns documented in-file:

- **`--tools` filters extension tools too**, not just builtins. An agent whose
  `harness_engineering` extension registers a tool **must name that tool in its own `tools` list**,
  or the extension loads and its tool is silently filtered out.
- **Bare model patterns fail validation.** `gemini-3.6-flash` matches three catalog entries across
  providers and `agents.validate()` refuses to spawn. Always write `provider/model-id`.

### 7.2 The roster — five agents

| Agent | Model | `writes:` | Purpose |
|---|---|---|---|
| `planner` | `fireworks/…/kimi-k3`, thinking high | `specs/` | Turn a request into a plan the builder can implement without asking questions |
| `builder` | default (gemini-3.6-flash) | *(absent = unrestricted)* | Implement the plan exactly; report every changed file |
| `scout` | default | `[]` (read-only) | Find and report where things live; change nothing |
| `reviewer` | `openai/gpt-5.6-terra`, thinking high | `[]` (read-only) | Confirm what was built is what was asked for |
| `documenter` | `openai/gpt-5.6-luna` | `app_docs/`, `docs/`, `**/*.md`, `*.md` | Write up the change from the diff |

A full entry:

```yaml
  - name: planner
    model: fireworks/accounts/fireworks/models/kimi-k3
    thinking: high
    color: "#a78bfa"               # the agent's lane color in the visualizer
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user:   adws/adw_data/prompt_engineering/planner/user.md
    harness_engineering:
      - adws/adw_data/harness_engineering/subagents.ts
    writes:                        # the plan is the only thing it may leave in the repo
      - specs/
    tools:
      - read, grep, find, ls, bash, write      # no edit
      - subagent_create, subagent_continue, subagent_list, subagent_remove
```

There is deliberately **no tester agent**, marked by a comment where one would go: *"running the
suite is a known command, so it is a `kind="code"` phase over `adw_modules/quality.py`. See
SKILL.md hard rule 8."*

### 7.3 `writes:` is the boundary — **[CORRECTS revision 1 and 2]**

Earlier revisions of this document claimed *"tool allowlists are a security boundary, and they're
used as one."* **That is exactly the claim the repo refutes.** From `permissions.py`:

> `tools:` is a capability list, not a sandbox, and two holes make it unenforceable on its own:
> - **`bash` runs anything.** A builder handed bash to run a test suite can also run
>   `git checkout adws/` — *which is not hypothetical: one did, discarding uncommitted changes to
>   the very quality check it was about to be judged by.*
> - **`write` reaches any path**, not just the one report file an agent was given it for. A
>   reviewer configured with "no edit, so it cannot quietly fix" could still rewrite the code it
>   was reviewing.

So permission is verified the way every other claim is — **after the fact, against the repo**:

| Function | Does |
|---|---|
| `snapshot(run)` | Fingerprints every path the working tree differs on (numstat for tracked, name for untracked) |
| `changed_paths(before, after)` | Every path that appeared, vanished, or was rewritten |
| `permitted(path, agent, cfg)` | Session runtime first → agent's own `writes` → `protected_files` → `writes is None` |
| `enforce(run, phase, agent, before)` | Compares, rolls back unauthorized changes, raises `PermissionBreach` |

Design points worth copying:

- **Comparing change-sets, not watching writes**, is what catches the `git checkout` case: a path
  modified before the agent ran and clean afterwards has been *reverted*, and a reversion is a
  modification.
- **A breach is not a gate violation.** Gates are for work an agent can redo; a breach cannot be
  corrected by re-prompting because the write already happened. It aborts the phase.
- **Rollback only undoes what the agent introduced.** A path already dirty when the agent started
  is left alone — discarding the operator's uncommitted work to tidy up would be the same harm the
  module exists to prevent. If the agent reverted pre-existing work, it says
  `REVERTED-BY-AGENT (uncommitted work lost, cannot restore)` loudly rather than pretending.
- **`*` stops at `/`; `**` crosses directories.** Custom glob, because `fnmatch` would let
  `adws/adw_*.py` match `adws/adw_data/sessions/x/y.py`.
- **Semantics:** `writes` absent = unrestricted · `[]` = read-only w.r.t. the repo · `[...]` = only
  those paths. Naming a protected path in your own `writes` is what unlocks it.
- **The session runtime is always writable.** `writes: []` means read-only *with respect to the
  repo*, never mute — every agent can always write its own report.

The one-line summary: *"An agent must not be able to edit the machinery that decides whether its
own work passed."*

### 7.4 The core four

| Dial | YAML |
|---|---|
| **Model** | `model` + `thinking` |
| **Prompt** | `prompt_engineering.system` + `.user` |
| **Harness** | `harness_engineering[]` — pi extensions |
| **Tools** | `tools[]` allowlist |

> *"Turn one dial, not the whole system. One entry in AGENTS = four independent inputs."*

---

## 8. The handoff contract **[SHOWN — references/handoff.md]**

### 8.1 Two output channels, exactly

1. **Reference files** written into `context_handoff/` — plans, notes, artifacts for later agents.
2. **A final valid-JSON response** — the envelope, and nothing else.

Code does the rest: parse against the declared type, persist as `envelope.json`, inject into the
next agent's user prompt.

### 8.2 Envelope schema

```python
class EnvelopeBase(BaseModel):
    status: Literal["success", "fail"]  # the only required field
    summary: str = ""                   # one sentence: what happened
    artifacts: list[str] = []           # paths written, usually inside context_handoff/
    notes_for_next_agent: str = ""      # what the next agent must know
```

**`status` is load-bearing:** an envelope that parses but reports `status="fail"` **raises**,
failing the phase. An agent declaring its own failure is not a successful phase.

| Type | Adds |
|---|---|
| `GenericOutput` | — (fallback) |
| `PlanOutput` | `commit_message` (describes the plan file) |
| `BuildOutput` | `changed_files[]`, `commit_message` (describes the code) |
| `ScoutOutput` | `findings[]` — `{file, note}` |
| `ReviewOutput` | `approved: bool`, `findings[]` — `{requirement, met, evidence}`, `blocking[]` |
| `DocumentOutput` | `document_path`, `documented_files[]`, `commit_message` |

Two are **adapters**, not agent reports — code shaped as an envelope so an agent can be handed a
deterministic result through the same door: `VerifyOutput` (a lint/test block) and `ChangesOutput`
(a captured diff). *The consuming agent cannot tell the difference, which is the point.*

There is no test output type — the suite is a `kind="code"` phase and its `QualityResult` reaches
the next agent via `quality.as_envelope`.

### 8.3 Parse failure is not a restart

If the response doesn't parse or validate, the harness re-prompts the **same session** with a
correction naming the required fields — bounded by `JSON_FIX_ATTEMPTS = 2` in `agents.py`. Gate
violations use the identical mechanism, bounded by the phase's `retries`. *"A cold restart would
throw away the context that produced the near-miss."*

Pi's `--session-id` is create-or-continue, so running an agent and continuing it are the same call.
The parser tolerates a fenced ```json block or surrounding prose, but the prompt still asks for
bare JSON, and every failed attempt persists as an invalid envelope row.

### 8.4 Prompt templating

`prompts.py` renders `user.md`, substituting:

| Placeholder | Value |
|---|---|
| `{{prompt}}` | the engineer's ask (or the ADW's per-call prompt) |
| `{{previous_envelope}}` | upstream envelope JSON, from `AgentCall(previous=…)` |
| `{{context_handoff_dir}}` | absolute path to this session's `context_handoff/` |

`user.md` structure: one `###` per incoming datum → `## Task` → `## Report` (the exact JSON shape
of the declared output type). **`system.md` stays static: Purpose + Instructions only.**

The split is the point: *identity* lives in `system.md`, *task shape* lives in `user.md` + the
output type at the call site. That's what lets one agent serve many calls.

### 8.5 Session directory layout

```
adws/adw_data/sessions/{adw_id}/
├── agent_map.json          agent name → coding-agent session_id + model
├── context_handoff/        the ONE place agents write files for the agents that follow
└── {agent_name}/
    ├── prompts/            exact prompts sent, saved before execution
    ├── pi_sessions/        pi's own session state
    ├── raw_output.jsonl    full JSONL stream, appended live
    └── envelope.json       the final validated response
```

### 8.6 Resuming across ADWs

`agent_map.json` lets a later ADW rejoin each agent's **existing context window**:

```bash
uv run adws/adw_plan.py "add dark mode"           # mints adw_id a1b2c3d4
uv run adws/adw_build.py --adw-id a1b2c3d4        # builder resumes, not cold
```

If config drift changes an agent's model, that agent starts a **fresh** session and the map is
updated — never a bad resume.

---

## 9. Gates **[SHOWN — gates.py]**

`gate(envelope, run) -> GateReport`, one `{item, ok, note}` check per thing examined. Violations
are *derived* from failed checks. **A green gate says what it verified**, not just that it passed.

| Gate | Checks |
|---|---|
| `artifacts_exist` | Each declared artifact exists (note carries the file size) |
| `files_non_empty` | Declared artifacts aren't zero-byte (existence is the other gate's job) |
| `json_parses` | `.json` artifacts actually parse |
| `diff_matches_claims` | Every file claimed changed exists on disk |
| `verdict_consistent` | A review's verdict agrees with its own findings |
| `tests_pass(command)` | **Gate factory** — the shell command must exit 0; failure keeps the last 1000 chars as evidence |

`verdict_consistent` is the sharpest, and it judges nothing about the code:

> An approval that ships blocking items, or a rejection that names no problem, is a claim the
> harness can refute without reading a line of the diff.

Three checks: `approved vs blocking`, `approved vs findings` (unmet requirements),
`rejection names a problem`.

**Gates check what is mechanically checkable; plan quality is a reviewer's job.**

---

## 10. Observability **[SHOWN — references/observability.md]**

One data path: **agents → sqlite → web UI**. No push, no ingest endpoint, no WebSocket.

**Two stores, one truth.** Files are the raw record (`raw_output.jsonl`, `envelope.json`,
`agent_map.json`); SQLite is the queryable mirror. `tracer.py` writes both. *Losing the db loses
nothing that can't be rebuilt from files.*

### 10.1 Event types

`phase_start` · `agent_start` · `tool_call` · `handoff` · `gate_pass` · `gate_fail` · `log` ·
`agent_end` · `phase_end` · `error` — every one logged against `adw_id` **and** `phase_id`, with
`parent_id` for span nesting.

- **`tool_call` is the only event that spans time** — it fills both `started_at` and `ended_at`.
  Everything else is a point. Lay tool calls out from those columns, never by parsing the payload.
- **Streaming is solved by construction:** `agent_pi.py` tails pi's JSONL stdout line by line and
  the tracer inserts *while the agent is still working*. Never batched at phase end.
- **Spend is itemised per phase.** `agent_end.usage` carries tokens *and* dollars per component
  (`input`, `output`, `cache_read`, `cache_write`), summed across every send — so a phase that
  retried shows what all attempts cost. `reasoning_tokens` is *inside* `output_tokens`, not a
  fifth component.
- **Context is occupancy, not spend.** `events.tokens` only grows; `context_tokens` is how full
  the window was when the agent stopped, measured against `context_window` from
  `~/.pi/agent/models.json`.

### 10.2 Seven tables

`sessions` · `phases` · `events` · `envelopes` · `gate_results` · `processes` · `agent_sessions`

Two worth calling out:

- **`phases.status` defaults to `'fail'` — success must be earned.** Only a clean exit writes
  `success`; agent phases additionally need the envelope parsed and gates green.
- **`processes`** maps `adw_id → pid` for both the ADW and its agent children. *"A hung agent emits
  nothing, which is exactly when you need its pid."* `just procs` lists what's live, `just kill`
  stops children before the parent, and both verify the recorded `command` still matches the pid
  before signalling. SIGTERM/SIGINT become `SystemExit` in `session.ensure`, so a killed run
  finalizes its own trace to `fail` instead of reading `running` forever.

**Derived, never stored:** phase durations, session progress, lane layout.

### 10.3 WAL and polling

Open **every** connection — writer and reader — with:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

Live view polls on a rowid cursor every `poll_ms`:

```sql
SELECT ... FROM events WHERE adw_id = ? AND rowid > ? ORDER BY rowid LIMIT 500;
```

History is **the same query** with filters — one mechanism serves live and past runs, which is why
there's no separate replay path.

---

## 11. The operator skill

### 11.1 Startup discipline — worth stealing wholesale

`SKILL.md` tells the orchestrator to do exactly three things at startup: read the overview, list
the ADWs, print them as a table, **and wait.** Then:

> **Nothing else.** No trace-db queries, no reading the config or the ADW scripts' bodies, no repo
> inventory, no last-runs summary, no "current state" dashboard.

With reasons that generalize far beyond this project:

- **Volunteered state is guessed state.** An orchestrator that improvised a status board queried a
  `runs` table and a `payload` column — neither exists (`sessions`, `payload_json`). *"Probing to
  look prepared is how you end up confidently wrong in your first message."*
- **It spends the context the real task needs**, before you know what the task is.
- **It is stale on arrival.** State printed before the request describes a system the very next run
  changes.

### 11.2 Orchestrator rules

- Never implement, plan, or test in an agent's place — launch the ADW and watch it.
- Never edit files inside `sessions/` — that is the run record.
- Query the db **when observing is the task**, never to volunteer a status report nobody asked for.

### 11.3 Lazy routing

| Request | Cookbook |
|---|---|
| `/sssf install` | `install.md` |
| create a new ADW | `create_adw.md` |
| modify an existing chain | `update_adw.md` |
| create the config / roster | `create_config.md` |
| add or retune an agent | `update_config.md` |
| extend `adw_modules` | `update_modules.md` |
| run / monitor an ADW | `how_to_prompt_for_the_eng.md` **first**, then `run_adw.md` |

Deep specs (`references/config.md`, `handoff.md`, `observability.md`) load only when needed.
*"Reading it early defeats the mechanism."*

---

## 12. Getting started

```bash
# 1. get the skill into the target repo
git clone https://github.com/disler/super-simple-software-factory
cp -r super-simple-software-factory/.claude/skills/sssf your-repo/.claude/skills/

# 2. stamp the factory — run from the target repo ROOT
cd your-repo && uv run .claude/skills/sssf/scripts/install.py

# 3. configure
cp .env.sample .env        # set OPENROUTER_API_KEY
pi --version               # Pi on PATH? else set PI_PATH in .env
pi --list-models           # does google/gemini-3.6-flash resolve?

# 4. smoke test
just demo
uv run adws/adw_prompt.py "reply with a one-line summary of this repo"
sqlite3 adws/adw_data/sssf.db "select adw_id, status from sessions order by started_at desc limit 1;"
```

Green means the whole path works: config validated, session minted, Pi ran, envelope parsed,
events landed. **Fix the smoke test before composing chains** — every multi-agent ADW rides this
exact path.

### 12.1 Customization order — what pays off fastest

| Change | File | Why |
|---|---|---|
| **Your real commands** | `adws/adw_modules/quality.py` | ⚠️ **The shipped blocks are placeholders that exit 0.** Until you wire this, your test phase is theater |
| Your prompts | `adws/adw_data/prompt_engineering/{agent}/` | Where your standards live |
| Your roster | `adws/adw_sssf_config/sssf.config.yaml` | Models, thinking, tools, `writes` |
| Your chains | `adws/adw_*.py` | Copy the closest and edit the phase list — 40–180 lines on purpose |
| Your definition of done | `adws/adw_modules/gates.py` | A gate is one function |
| Your agent capabilities | `adws/adw_data/harness_engineering/` | Pi extensions, per agent |

That first row is the highest-severity gotcha in the whole system. `adw_build_test`,
`adw_plan_build_test`, and `adw_simple_sdlc` all run those placeholders as their test phase, and a
green placeholder means the SDLC's `verified` flag is meaningless.

---

## 13. Known failure modes **[SHOWN — README]**

| Failure | What happens | Fix |
|---|---|---|
| Test phase green on fresh install | `quality.py` placeholders exit 0 | Wire real commands **first** |
| Bare model pattern | Matches several providers; `agents.validate()` refuses to spawn | Always `provider/model-id` |
| `just` missing | Nothing depends on it | Run the one-line recipe yourself |
| Coding agent hangs | No events, no tokens, empty `raw_output.jsonl` — trace goes quiet, not red | Query `processes`, kill children-first |
| Synced triad drifts | Type / `## Report` / `output_type=` disagree → correction rounds burn | Grep the type name, fix all three |
| Gates pass, output bad | Gates check predicates, not taste | Run the reviewer, or read it |
| Agent edits what it shouldn't | Rolled back, phase fails | Expected — widen that agent's `writes` if legitimate |
| Commit phase has nothing | `commit_all` raises outside a repo or on no-op | `git init` + one commit first |
| `install.py --force` | Overwrites **all** stamped files, config and prompts included | Commit before forcing |
| `coding_agent: claude_code` | Schema-valid, `agent_cc.py` raises | v1 is Pi only |

### 13.1 Missing on purpose

Runs on your current branch. **No sandbox, no branch per run, no merge step, no cloud, no
human-in-the-loop approval phase.** Left out so the core stays small enough to read in one sitting
— *"which is the only reason you would trust it enough to change it."*

Add yourself, and design deliberately: secret handling in sandboxes, per-run cost ceilings,
concurrency limits, and what happens when `MAX_FIX_LOOPS` exhausts (currently: loop exits,
`verified` goes false, run rejected — fine for one repo, thin for a fleet).

---

## 14. Build order (if you're writing your own rather than stamping this)

1. **`data_types.py`** — `EnvelopeBase` + your output types. Everything depends on the handoff shape.
2. **`session.py` + the `Phase` context manager** — entry/exit hooks for validation, cost, events.
3. **`tracer.py` + SQLite + WAL.** Fourth at the latest — you cannot debug anything past here
   without it.
4. **`adw_prompt.py`** — one agent, one prompt, typed envelope out. The atom.
5. **Config loader + `agents.validate()`** — two agents to start: planner, builder.
6. **`gates.py`** — `artifacts_exist`, `files_non_empty`.
7. **`adw_plan_build`.** First real leverage.
8. **`quality.py`** with your *real* commands, as a `kind="code"` phase + bounded fix loop.
9. **`adw_plan_build_test`.** The workhorse.
10. **`permissions.py`** — `writes` + `protected_files` + rollback. Before you let anything run
    unattended.
11. **`diff_matches_claims`**, reviewer, revise loop, **retest guard**, documenter → `adw_simple_sdlc`.
12. **The operator skill** — hard rules + cookbooks + routing table + startup discipline.
13. **Then** the gaps in §13.1: branch, sandbox, isolate, merge, cloud.

---

## 15. Design principles

- **Agent proposes, code disposes.**
- **A known command is code, not an agent.**
- **`tools:` is capability; `writes:` is the boundary.** An agent must not be able to edit the
  machinery that decides whether its own work passed.
- **Verify claims after the fact, against reality** — envelopes, diffs, and permissions are all
  checked against the repo, not trusted from the model.
- **A breach is not a violation.** Redoable work gets re-prompted; an already-happened write gets
  aborted.
- **Success must be earned** — `status` defaults to `fail`.
- **Re-verify after any change.** A green light from before the last edit is not a green light.
- **Failure commits nothing** — except the plan, which records what was asked.
- **Store whole, clip only pathologically.**
- **Don't volunteer state.** Guessed status is worse than no status.
- **One agent, one prompt, one purpose.**
- **Ship defaults, expect replacement.** *"Nothing here is meant to survive contact with your
  codebase unchanged."*
- **Design for the thousandth run.**

---

## 16. The honest objection

From the README, unprompted:

> **Is this overkill for a one-off feature?** Yes. Prompt an agent and move on. This earns its
> keep when the same workflow runs a hundred times, when validation is the only thing standing
> between you and a bad merge, and when you need the thousandth run to look like the first.

> "Vibe coding is not knowing how your system works and not looking. Agentic engineering is
> knowing how your system works so well you don't have to look."

---

## Appendix: what changed from revision 2

| Claim in rev 2 | Reality |
|---|---|
| Repo contains a working `adws/` tree | Repo is the skill only; `templates/` are stamped by `install.py`. The `example` branch has it populated |
| "Tool allowlists are a security boundary, and they're used as one" | **Wrong** — `permissions.py` exists precisely because they can't be. `writes:` + `protected_files` + post-hoc rollback is the boundary (hard rule 9) |
| Only hard rule 8 known | All ten recovered |
| `quality.run_inkwell_tests(run)` | `quality.run_tests(run)` in the template — the video showed his demo-specific variant |
| `owner="engineer"` | `owner=run.engineer` |
| Missed the `commit_build` phase | Code commits *after* verification; three commits total |
| Gates return violations | Gates return `GateReport` with per-item evidence; also `json_parses` and the `tests_pass(cmd)` factory |
| Loop bounds unknown | `MAX_FIX_LOOPS=3`, `MAX_REVISION_LOOPS=2`, `JSON_FIX_ATTEMPTS=2` |
| Reviewer/documenter models unknown | `openai/gpt-5.6-terra` and `openai/gpt-5.6-luna` |
| DB schema unknown | Seven tables, full schema, WAL pragmas, rowid-cursor polling |
| Didn't know about the placeholder-`quality.py` trap | The single highest-severity gotcha |

---

## References

- Repo: https://github.com/disler/super-simple-software-factory · [`example` branch](https://github.com/disler/super-simple-software-factory/tree/example)
- Video: https://youtu.be/haUfb1ievTE
- In-repo specs: `references/config.md` · `references/handoff.md` · `references/observability.md`
- Course referenced in the video: Tactical Agentic Coding (`agenticengineer.com`)
