# Factory — Session Handoff (2026-08-13)

**Read this first in a fresh session.** It captures where we are, what's next, and every credential/location you need. Companion memory: `MEMORY.md` + `saipien-software-factory.md`, `buzz-collaboration-layer.md`, `slate-os-buildops-mapping.md`.

---

## TL;DR — where we are
The agentic factory is **built, hardened, and PROVEN end-to-end on a persistent box (the VPS).** A full JRN-S2 journey ran to a real verdict: OMP authed remotely with both subscriptions, the Budget fix stopped the builder churn, the Isolation port held (origin never touched), gates enforced, and the **independent cross-family reviewer caught a real bug** (a missing publish migration) → correct escalation. That escalation is now **resolved** (DEC-058 + migration `0003`, ratified + committed).

**We are moving from the interim "run-on-the-VPS" model to the production architecture: a persistent brain (VPS) orchestrating disposable ephemeral workers (exe.dev).** This is the IDD "factory-in-a-box," recentered.

---

## THE ARCHITECTURE (recentered on IndyDevDan's model)
- **Client** — Christian's workstation + Claude (this CLI). Directs/observes; nothing critical runs here (it idle-suspends).
- **Persistent brain — the Hostinger VPS** (`5.181.218.246`): Buzz relay + observatory + the orchestrator + the **OMP `auth-broker`**. Always-on.
- **Ephemeral workers — exe.dev**: one disposable VM per run (the real "in-a-box"). Behind the **Isolation port** (`control-plane/isolation.py`), which already has a git-worktree adapter; the exe.dev adapter is the next build.
- **Origin of truth → GitHub** (production-shaped). Isolation "merge-back" becomes a `git push`.

---

## NEXT UP — Priority #2: the exe.dev ephemeral worker model
Four pieces, in order. **Start with the auth-broker (self-contained, it's the gate).**

1. **OMP `auth-broker` on the VPS.** ⚙️ **Largely DONE (2026-08-13) — see `ops/AUTH-BROKER-RUNBOOK.md`.**
   - **Root cause found + fixed:** Anthropic uses a *rotating* refresh token; the workstation + VPS held the **same copied** token, so each refresh invalidated the other → recurring outage. Both copies were dead this session. **Fix = single-refresher discipline:** the VPS broker is the *only* holder/refresher; everything else borrows. Christian re-authed Anthropic on the VPS broker (verified: `credential disabled`=0, codex healthy).
   - **Broker is a durable systemd service:** `omp-auth-broker.service` **active + enabled** (boot-persistent, `Restart=always`), bound `127.0.0.1:9099`, bearer-gated (borrow path returns `unauthorized` without `/root/.omp/auth-broker.token`). Unit version-controlled at `ops/omp-auth-broker.service`.
   - **Factory enforces borrowing in code:** `control-plane/omp.py:_child_env()` loads `OMP_AUTH_BROKER_URL`/`_TOKEN` from a broker-env file into every spawned omp (fail-open if absent; unit-tested). No VPS profile hooks needed.
   - **Env vars confirmed (from the omp binary):** `OMP_AUTH_BROKER_URL`, `OMP_AUTH_BROKER_TOKEN` (+ `OMP_AUTH_BROKER_SNAPSHOT_CACHE`/`_TTL_MS` client cache for resilience, `_ACCOUNT_POOL_FILE` for multi-account balancing later).
   - **⬜ Remaining (Christian runs the deploy block in the runbook — auto-mode blocks Claude from writing files to the VPS):** create `/root/.omp/broker-client.env` + install the **watchdog timer** (`ops/omp-broker-watchdog.*` + `ops/broker-watchdog.sh`) that posts a proactive Buzz alert to #factory-escalations the moment any credential fails. Then `omp auth-broker migrate`/`~/.codex` carry is **moot** — creds already live on the box and the broker vends both.
2. **Bootstrap setup-script** (≤10KiB, passed to `new --setup-script`): install uv/node/omp, clone factory + target repo from GitHub, point OMP at the broker (`OMP_AUTH_BROKER_URL` env), run the lane, `git push` the accepted merge, post to Buzz, write a completion marker.
3. **Remote runner** (new orchestrator mode / adapter): create an exe.dev VM per ready journey (`new --memory=4GB --setup-script=… --env …`), poll for completion, then `rm` the VM. exe.dev in-VM control is **batch only** (SSH-22 is blocked from here; the per-VM HTTPS proxy is for services, not exec) — so the VM self-runs the lane via the setup-script and reports OUT.
4. **GitHub push flow** — the worker clones the repo from GitHub and pushes the accepted branch back. Needs a GitHub token on the worker (repos are private).

### Then #3: re-dispatch JRN-S2 / JRN-B3 on exe.dev → **first full GREEN** (accept + merge). We've only ever seen escalate; a clean accept closes the loop. **Prereq: push Arxus `main` (d2a106d) to GitHub first** (currently committed locally only — confirm with Christian before pushing).

---

## PRIORITY ORDER (agreed)
1. ✅ **Draft + ratify the publish migration** — DONE (DEC-058, `0003_publish_listing_fn.sql`, committed to Arxus `main` `d2a106d`).
2. **exe.dev ephemeral workers** ← *you are here* (steps above).
3. **Re-dispatch JRN-S2/JRN-B3 on exe.dev → first green.**
4. **Durable VPS access** — Tailscale (the apt install failed mid-way; retry). SSH-on-:2222 did **not** work (banner-exchange timeout — network interferes beyond :22). Auth key `tskey-auth-k66FxPitc311CNTRL-…` was provided (may be single-use/expired — get a fresh reusable one). Until then: **VPS SSH needs Christian's VPN up.**
5. **Run-speed tuning** — a single journey is ~50 min (each opus/gpt phase 10–20 min). Later: phase-level parallelism, tighter builder prompts, maybe lower planner thinking.
6. **Live copilot (`buzz-acp` on VPS)** + the **2 pre-launch ratifications** (OPEN-S3-1 rubric, OPEN-VAL1 benchmarks) + **refresh the design artifact** (it's stale — shows the old serial 7-stage model; update to the concurrent tracks-×-phases model + ontology fixes).

---

## ARXUS (the product the factory builds) — build status
Repo `github.com/cryptocrystian/arxus`, `main` = **d2a106d (pushed 2026-08-13)**. **Foundation solid, product surface early — ~2 of ~22 journeys built.**
- **Built + factory-accepted (merged):** the full **canon** (DEC-052→058), the **schema** (migrations `0001` 31-table/RLS/invariants, `0002` LV-immutability, `0003` atomic publish), the **auth foundation** (Supabase AuthProvider, DEC-056 seam), **JRN-S1** (valuation → emailed result, 37/37 tests), **JRN-S3** (exit-readiness assessment). `lib/`: valuation, assessment, auth, email, events, supabase. 13 routes / 35 lib / 15 components / 25 tests.
- **Ready to (re)build — blockers resolved in canon, never merged:** **JRN-S2** (published seller + live listing — immutability fixed by `0002`, publish by `0003`/DEC-058) and **JRN-B3** (NDA → tiered docs — e-sig DEC-057, concurrency fix approved). Both are `ready` in `backlog.yml`. **This is priority #3.**
- **Not started (~18 journeys):** offers/deals (S5,B4,T1), notes (T2,N1,N2), buyer BQS (B1), saved search/match (B2,M1), jurisdiction (J1), financing (F1), broker (BR1), AI-approval (AI1), etc. — the bulk of the product.
- **Deferred pre-launch sign-offs (NOT build blockers):** OPEN-S3-1 (ratify readiness rubric), OPEN-VAL1 (ratify valuation benchmark table + counsel on SBA). Owner sign-off before real-user exposure.
- **Detailed Arxus state lives in the repo:** `canon/Decision Log.md`, `canon/Canonical Journeys v2.md`, `canon/Acceptance Criteria v2.md`, plus the factory's `backlog.yml` + `runs/decisions.yml`. Reconstruct anytime from `git -C ~/projects/arxus log`.

## CREDENTIALS & LOCATIONS
- **VPS:** `root@5.181.218.246` (`srv816212.hstgr.cloud`), key `~/.ssh/factory_vps`. **SSH is VPN-gated** (Christian's network blocks outbound :22). On-box Claude Code (`claude` v2.1.229) is an alternative for VPS-side work. Up 146 days; 6GB free RAM, 80GB disk.
  - Installed there: `omp` (`/usr/local/bin/omp`, both families authed via copied `~/.omp` + `~/.codex`), `uv` (`/root/.local/bin/uv`), node18, git, python3, docker, Claude Code.
  - `/root/factory` (staged factory code), `/root/arxus` (staged — **STALE at 1cbd19e**; main is now d2a106d; owned-by-`ubuntu` uid so `git config --global --add safe.directory '*'` was set). `/root/validate_0003.sh`. `/root/.config/buzz/operator-agent.env`.
- **exe.dev:** token in `~/.config/exe/token` **and** `~/factory/.env.local` (`EXE_TOKEN`). API: `curl -X POST https://exe.dev/exec -H "Authorization: Bearer $(cat ~/.config/exe/token)" -d '<cmd>'`. Commands: `new/ls/rm/cp/resize/stat` (billing scoped-out — fine). VMs: 4GB/2CPU/25GB, ~1s boot, `boldsoftware/exeuntu`, `--setup-script`/`--env` supported. No VMs currently running (spike `fspike` reaped).
- **Buzz:** relay `wss://buzz.saipienlabs.com`; operator creds `~/.config/buzz/operator-agent.env` (`BUZZ_PRIVATE_KEY`, `BUZZ_RELAY_URL`); client `control-plane/buzz.py` (NIP-98, crypto verified via `python3 buzz.py cryptotest`). Owner = ChristianD (imported the owner nsec). Channels: `factory-escalations` `cee4c3ba-…`, `arxus` (home) `c050feab-…`, `arxus-product` `2add2a70-…`. Owner-key file was at `~/factory/ops/owner.key` — **should be shredded** if still present (it's gitignored).
- **OMP:** models opus-5 (planner/builder), gpt-5.6-sol/terra (test/reviewer — DIFFERENT family = I3). Auth: Anthropic `cdibrell@gmail.com` (OMP vault) + openai-codex `cdibrell@gmail.com (plus)` (from `~/.codex/auth.json`). Budgets (in `control-plane/config.py`): builder `medium`/1500s, planner 900s, test 900s, reviewer 600s; `Budget.max_wall_s=2700`, `max_retries=2`.
- **GitHub:** `github.com/cryptocrystian/factory`, `github.com/cryptocrystian/arxus`. **Arxus main d2a106d is committed locally, NOT pushed** — push before the exe.dev re-dispatch (confirm with Christian).
- **MCP connectors dormant:** `exa` + `supabase` need auth via `/mcp` in an interactive session. Supabase would help apply/test migrations directly.

---

## WHAT WAS BUILT/PROVEN THIS SESSION
- **Phase-0 hardening (all verified):** agent-phase gates now ENFORCE on live runs (`feature.py`); `diff_matches_claims` checks real diffs (`gates.py`); **Budget port** (per-role timeouts + wall cap); **BIP-340 known-answer crypto vectors** (`buzz.py cryptotest`); honest gated merge (residue capture + I11 refusal, moved into the isolation adapter); MANIFEST reconciled.
- **Isolation port** (`control-plane/isolation.py`): git-worktree adapter (acquire/merge_back/destroy); process-group kill in `omp.py` + `gates.py` (fixes the prior run-interruption). K7 rewritten for worktree semantics. **All selftests pass** (`selftest_k1/k2/k7`, `buzz.py cryptotest`).
- **BuildOps collaboration layer:** `canon/buildops-canon.md` (SoR, ontology, naming, concurrent tracks-×-phases, B1–B11 invariants), `taxonomy.yml`, `provision.py` (idempotent channel provisioner — provisioned `arxus` + `arxus-product`), Notifier facet-routing (`notifier.py` + `notify.yml`).
- **VPS validation:** full JRN-S2 ran end-to-end on the VPS → escalated correctly on the publish/migration finding (a *real* bug caught by the cross-family reviewer).
- **DEC-058 + `0003_publish_listing_fn.sql`** — atomic publish, validated against real Postgres, committed to Arxus main.
- **Design artifact:** https://claude.ai/code/artifact/a6a155cb-5843-4266-bc92-b57697ba345a (BuildOps × Buzz) — **STALE**; needs the concurrent-model + ontology-fix refresh (priority #6).

---

## GOTCHAS / OPEN
- **Auth durability (2026-08-13):** never copy OMP OAuth to two machines — Anthropic's rotating refresh token guarantees they invalidate each other. The **broker is the single refresher**; all else borrows via `OMP_AUTH_BROKER_URL`/`_TOKEN`. Health check: `journalctl -u omp-auth-broker --since "$(systemctl show -p ActiveEnterTimestamp --value omp-auth-broker)" | grep -c 'credential disabled'` (0 = healthy). Re-auth runbook in `ops/AUTH-BROKER-RUNBOOK.md`.
- **Before exposing the broker to exe.dev over Traefik/HTTPS:** first **verify a borrow returns a short-lived token, not the raw OAuth** (so a compromised worker can't steal the master credential). Then add a Traefik router → `127.0.0.1:9099`, bearer-gated. Details in the runbook's exposure section.
- **Auto-mode blocks Claude from writing files to the VPS over SSH** (scp, heredoc-to-file, `>> .bashrc` all denied by the classifier) — read-only SSH and `systemctl` are allowed. Deploy VPS-side file changes via a copy-paste block for Christian, or add a Bash permission rule. (This is why the broker service is live but the client env-file + watchdog are a deploy block, not auto-applied.)
- **VPS SSH needs VPN up** (or use on-box Claude Code). Durable access (Tailscale) is unfinished.
- **Arxus on the VPS is stale** and **not pushed to GitHub** — reconcile before #3.
- The **direct JRN-S2 validation run** on the VPS (run via the lane, not the orchestrator) finishes `accepted=False` and does **not** post to Buzz (that's the orchestrator's job). Safe to ignore/clean its worktree under `/root/factory/runs/_worktrees/`.
- **Backlog** (`backlog.yml`): `jrn-s2` + `jrn-b3` = `ready`; `auth-foundation` = accepted; `ratify-rubric` + `ratify-benchmarks` = awaiting_human.
- **The factory is NOT in git** on the workstation (`~/factory` has a remote but changes this session are uncommitted). Consider committing the factory before/after #2.
- Standing constraints hold: greenfield-only; workstation is a client not a worker; tooling swappable but discipline (observable, gated, agent-proposes/code-disposes) is sacred.
