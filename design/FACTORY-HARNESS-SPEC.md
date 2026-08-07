# Factory Harness — Decision & Agent-Port Adapter Spec

**Date:** 2026-08-07 · **Status:** decided (Open Decision #5 resolved) · design artifact, no code yet.
Companion to `AGENTIC-SOFTWARE-FACTORY-PLAN.md` (Rev 4) and
`super-simple-software-factory-implementation-guide.md`.

---

## 1. Decision (resolves Open Decision #5)

**Adapt disler's `sssf` control plane; implement the Agent port with Oh My Pi (OMP) run in a
constrained "executor" mode.**

- The **control plane** (phases, typed envelopes, gates, retries, budgets, write-enforcement,
  commit/merge, tracing) is code we own — adapted from `sssf`'s `adw_modules/`. This is where the
  factory's value lives; it is not delegated to any harness (Rev4 **I1**: control flow is code).
- The **Agent port** — `(context, model, prompt, tools, writes, pool) → typed envelope` — is
  satisfied by invoking **OMP** as a subprocess per phase, replacing `sssf`'s bare Pi (`agent_pi.py`
  → `agent_omp.py`).

**Why OMP over bare Pi** (all verified against the local install, `omp v17.2.10`):

- Multi-provider incl. `ANTHROPIC_OAUTH_TOKEN` — the cross-family judgment mandates (reviewer ≠
  builder, ≥2-family L4 panel) become config, and Anthropic-heavy roles run on **subscription**, not
  metered API (serves Rev4 §12 economics).
- `auth-broker` / `auth-gateway` / `dry-balance` (OAuth account balancing) ≈ the "subscription-first,
  spill-to-metered" capacity broker from §12 — already built.
- `--mode json | rpc | acp` — a structured channel for typed-envelope plumbing, cleaner than tailing
  Pi's raw JSONL.
- First-class `modelRoles` config + `--smol/--slow/--plan`; native git `worktree` management (for
  P4/P5 isolation); `tools.abortOnFabricatedResult` + `intentTracing` integrity instrumentation.

**The governing constraint:** OMP is a *maximal, autonomous* harness; the factory's philosophy is the
opposite (minimal harness, code drives). We use OMP's **capability** and reject its **autonomy**. Its
autonomous-workflow features are opt-in and default off — we keep them off inside the factory.

---

## 2. The Agent port contract (recap)

One phase = one bounded OMP invocation. In: a system prompt (role identity, frozen), a user prompt
(task + upstream envelope), a tool allowlist, a model, a write grant. Out: exactly one typed envelope
(valid JSON, last message). The control plane parses it against the declared type, persists it to
`runs/<id>/`, gates it, and either advances or re-prompts the same session with corrections.

---

## 3. OMP invocation spec (per phase)

> **⚠ Verified (smoke test 2026-08-07):** in `-p` mode with a non-TTY stdin (any subprocess), OMP
> **reads the prompt from stdin and ignores the positional argument**, blocking on EOF until stdin
> closes. The adapter MUST feed the prompt via stdin (`printf '%s' "$prompt" | omp -p …`), not as a
> positional. Also pass `--no-title` (title auto-generation spawns an extra model call per phase).

Baseline flags for **every** factory phase (non-interactive executor):

| Flag | Value | Why |
|---|---|---|
| `-p` / `--print` | on | Non-interactive: process one turn and exit. **Prompt piped via stdin.** |
| `--mode json` | on | JSONL event stream (verified schema in §3a). |
| `--no-title` | on | Suppress the extra title-generation model call. |
| `--model` | per role (§5) | Role's assigned model. |
| `--thinking` | per role | e.g. `xhigh` planner/builder, `high` reviewer, `low` documenter/classifier. |
| `--tools` | per role allowlist | Capability list — the narrow set the role needs. |
| `--system-prompt` | role `system.md` | Frozen identity (prompt-cache stable — §7). |
| `--session-dir` | `runs/<id>/omp/<agent>` | Per-run session storage; enables the correction loop (§4). |
| `--no-skills --no-rules --no-extensions` | on | Nothing loads except what the factory injects. |
| `--cwd` | the run workspace | Worktree/container path. |
| `--max-time` | per budget | Hard wall-clock ceiling (Rev4 I9). |

**Deliberately OFF** (these move control into the harness — violate I1/I3):

- `--plan-yolo`, `--prewalk` / `task.prewalk` — harness planning + autonomous model-switching. The
  factory's plan and build are *separate gated phases*.
- `--advisor` (`advisor.enabled`) — a passive **same-model** reviewer. Not independent verification;
  cannot substitute for L4 (I3). At most a lint, never review.
- `--auto-approve` as a *substitute* for enforcement — see §4.
- `--continue` cross-phase context bleed — each phase is fresh except the intentional correction loop.

**Compaction:** irrelevant if phases stay bounded (the factory has no long context by design). Leave
default; a phase that would trigger it is too big and should be decomposed.

### 3a. `--mode json` event stream (verified)

JSONL, one event per line. Observed sequence: `session` → `agent_start` → (`turn_start` →
`message_start`/`message_update`/`message_end` → `tool_execution_start`/`_update`/`_end` → `turn_end`)×N
→ `agent_end`. What the adapter reads:

| Need | Where |
|---|---|
| **Session id** (for the correction loop) | first `session` event, `.id`. Matches the session filename under `--session-dir`. |
| **The envelope** | `agent_end.messages[-1]` (the last `assistant` message) — content is the model's final text. In the test it was **bare JSON**, no fence: `{"status":"success","summary":"…"}`. |
| **Terminal?** | `agent_end.isTerminal`. |
| **Tool-call spans** (for the tracer) | `tool_execution_*` — `toolCallId`, `toolName`, `args`, `intent`, `result`, `isError`. |
| **Usage & cost** (for the Budget port) | `.message.usage` on `message_end`/`turn_end`/`agent_end.messages[*]`: `{input, output, cacheRead, cacheWrite, totalTokens, cost:{…dollars per component…}, cttl}`. **`cacheRead` > 0 on later turns confirms prompt caching is live** — the §7 cost lever, directly measurable. |

---

## 4. The real boundary is NOT the approval prompt

OMP defaults to `tools.approvalMode = yolo` (auto-approve). For a *non-interactive* executor that is
acceptable and expected — **there is no human to prompt mid-phase.** Security does not come from an
approval dialog. It comes from two code-side mechanisms, exactly as in `sssf`:

1. **Tool allowlist** (`--tools`) — limits *which* tools exist for the role. But note the hole `sssf`'s
   `permissions.py` documents: `bash` runs anything and `write` reaches any path, so the allowlist is
   a capability list, **not a sandbox**.
2. **Post-hoc write-enforcement** (the actual boundary, Rev4 **I6**) — snapshot the tree before the
   phase, diff after, roll back any change outside the role's `writes:` grant, fail the phase on
   breach. Comparison (not write-interception) is what catches a `bash git checkout` reversion. This
   module is mandatory and is ported directly from `sssf`'s `permissions.py`.

So: harness auto-approves within a **narrow tool grant**; code enforces the **write grant** after the
fact. The builder still cannot touch `tests/`, `canon/`, or factory machinery (I3/I12) — because the
enforcer rolls it back, not because OMP refused it.

---

## 5. modelRoles mapping (Arxus P0)

Honors the family constraints from Rev4 §12. **Model IDs verified present via `omp models`
(2026-08-07).** Both judgment families are authenticated **on subscription** — Anthropic (Claude) and
OpenAI (Codex Plus) — so the cross-family calls cost subscription capacity, not metered list price.

| Role | Model | Family | Thinking | Pool |
|---|---|---|---|---|
| Classifier / triage | `claude-haiku-4-5` | Anthropic | low | subscription |
| Planner | `claude-opus-5` | Anthropic | high–xhigh | subscription |
| Criteria reviewer | `gpt-5.6-terra` | OpenAI · **≠ planner** | high | subscription (Codex) |
| Builder | `claude-opus-5` | Anthropic | xhigh → sweep down | subscription |
| Test author | `gpt-5.6-sol` | OpenAI · **≠ builder** | high | subscription (Codex) |
| Reviewer | `gpt-5.6-terra` | OpenAI · **≠ builder** (mandatory) | high | subscription (Codex) |
| Walker (L3) | `gpt-5.6-*` (vision) or Gemini | vision workhorse | medium | subscription/metered |
| Consensus panel (L4) | `claude-opus-5` + `gpt-5.6-sol` (+ Gemini when authed) | **≥2 families** | medium | subscription |
| Documenter | `claude-sonnet-5` | Anthropic | low | subscription |

Available frontier IDs seen: Anthropic `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`,
`claude-fable-5`; OpenAI (Codex) `gpt-5.6-terra`/`-sol`/`-luna`, `gpt-5.5`, `gpt-5.4`. Gemini is not
yet authenticated — add `GEMINI_API_KEY` to widen the L4 panel to a third family.

Config: set via `omp config set modelRoles …` with `modelRoleStorage = project` so the mapping lives
with the factory, not the user profile. With both families on subscription, the metered spill is
near-zero at P0 volume — better than the §12 baseline assumption.

---

## 6. The correction loop (gate failure → same session)

A failed gate returns to the **same** OMP session as a correction, never a cold restart (preserves the
context that produced the near-miss — `sssf` bounds this by `retries`).

Mechanism: the first call for a phase writes its session under `--session-dir runs/<id>/omp/<agent>`;
the adapter captures the session id from the first `session` event; a correction re-invokes with
`-r <session-id>` (resume) + the gate's verbatim findings as the new (stdin) user message. Bound by
the phase's retry count.

**Verified (smoke test 2026-08-07):** `-r <id-prefix>` **appends to the same session file** (session
count 1→1, no fork), the resumed run reports the **same session id**, and **context is intact** — the
resumed turn correctly answered a question about the prior turn's action. The correction loop is
buildable exactly as specced.

---

## 7. Prompt-cache discipline (unchanged from Rev4 §11 / §12)

Freeze each role's `system.md` and tool list — prompt caching is a prefix match; any byte change
invalidates the cache and silently multiplies cost. No timestamps/nonces in system prompts. Verify
`cache_read` tokens are non-zero across repeated runs (OMP surfaces usage per turn).

---

## 8. Adapter shape — `agent_omp.py`

Mirror `sssf`'s `agent_pi.py` interface so the rest of the control plane is unchanged:

- **Input:** `AgentCall` (output_type, prompt, previous envelope, gates, tools, model, writes).
- **Spawn:** build the flag set from §3 + §5; `subprocess` with `--mode json`, stream stdout.
- **Trace:** tail the json event stream line-by-line into the tracer *while the agent works* (same
  live-insert pattern `sssf` uses for Pi) — `tool_call` spans, `agent_end.usage` for per-phase spend.
- **Output:** parse the final JSON message against the declared `EnvelopeBase` subclass; on parse
  failure, re-prompt the same session (bounded, `JSON_FIX_ATTEMPTS`).
- **Enforce:** call the write-enforcement module (§4) after the phase; breach → abort phase.
- **Register the process** in the `processes` table (pid) so a hung agent is killable children-first.

---

## 9. Empirical verification — smoke test results (2026-08-07)

Run: `claude-haiku-4-5`, `-p --mode json`, restricted tools, throwaway dir. **All five confirmed.**

1. ✅ **`--mode json` schema** — clean JSONL; session id in the first `session` event; envelope is the
   last `assistant` message in `agent_end` (bare JSON). Full map in §3a.
2. ✅ **`-r` resumes, does not fork** — same session file appended, same id, context intact (§6).
3. ✅ **Subscription capacity** — Anthropic *and* OpenAI (Codex) both authenticated on subscription
   (`omp usage`); `--model` fuzzy-match resolved the intended IDs. `ANTHROPIC_BASE_URL` is the standard
   `https://api.anthropic.com`.
4. ✅ **`--tools` honored** — the run operated within `read,write,bash` and wrote the file via the
   granted `write` tool. (Full negative-deny test deferred; the allowlist was respected.)
5. ✅ **Usage/cost exposed** — per-message `usage` with dollar `cost` per component, and `cacheRead`
   went non-zero on the resumed turn → prompt caching demonstrably live (§3a, §7).

**Two adapter requirements discovered:** (1) prompt must be piped via stdin, `--no-title` set (§3);
(2) **OMP's tool vocabulary differs from sssf/Pi and `--tools` validates strictly (fails fast on an
unknown name).** There is no `ls`/`find` — directory listing is `glob`. The valid set includes:
`read, write, edit, bash, grep, glob, ast_grep, ast_edit, lsp, web_search, browser, computer, task,
todo, github, memory_edit, recall, …`. The adapter needs a fixed sssf→OMP tool-name map; the
read-only role set is `read, grep, glob` (+ `write` where the role emits a file).

Residual (not blocking the adapter): a full negative tool-deny test; behavior of a `bash git checkout`
reversion against the write-enforcer (that's a `permissions.py` test, not an OMP test).

---

## 10. Open / deferred

- ~~**Open Decision #6 — where the factory's code lives.**~~ **Resolved:** `~/factory`, its own repo
  (private remote `github.com/cryptocrystian/factory`), top-level and outside every venture (I12). This
  doc now lives at `~/factory/design/`.
- **Isolation (P4+):** OMP's native `worktree` for worktree-class work; container-in-worktree for
  L2–L3 remains remote (workstation is a client, not a worker). Keep P0 **single-stream** local — OMP's
  worktree + OAuth-balancing make parallel local runs tempting, and that's the WSL-destabilizing load.
- **`sssf` install prerequisite:** `sssf` v1 is Pi-only (`agent_cc.py` raises); adopting OMP means we
  author `agent_omp.py` rather than using a shipped adapter. Bare `pi` is not currently on PATH; OMP is
  the Pi we drive.
