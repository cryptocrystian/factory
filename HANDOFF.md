# Factory — Session Handoff (2026-08-18)

**Read this first in a fresh session.** Current state, what's live, what's next, every credential/location.
Companion: the auto-loaded memory `factory-handoff.md` (dated session notes with exact commands/recipes) +
`saipien-software-factory.md`, `buzz-collaboration-layer.md`, `slate-os-buildops-mapping.md`.

---

## TL;DR — the factory is now a self-running, parallel, governed system

Not a pipeline anymore. On the **persistent VPS brain**, a **continuous parallel daemon** (`arxus-factory.service`,
24/7, `Restart=always`) drains the Arxus backlog: it dispatches **up to N=2 journeys concurrently** (WIP + a §9
**decomposition gate** that only co-schedules journeys with disjoint canon `Touches:` bindings and serializes
overlapping ones), and it **never stops on a decision** — an escalation parks the item and the loop keeps
churning everything else. When a journey can't converge, an **architect agent** (in-loop, cross-family) resolves
the technical work itself — authoring migrations/canon the builder can't — and surfaces **only genuine
business/product decisions** to the human. Auth is durable (single-refresher broker), access is durable (Tailscale
tailnet), GitHub is origin of truth.

**Right now:** JRN-S4 + JRN-B1 are `in_progress` (building in parallel).

---

## ARCHITECTURE (current)
- **Client** — Christian's workstation + this Claude CLI (on the tailnet as `factory-workstation` 100.98.51.12).
  Directs/observes; the factory runs without it.
- **Persistent brain — the VPS** (`root@5.181.218.246`; tailnet `factory-vps` **100.103.168.32**): the
  orchestrator **daemon**, the OMP **auth-broker** (single refresher), Buzz relay + Traefik, and (for exe.dev) the
  tailnet-only broker forwarder. Always-on.
- **Ephemeral workers — exe.dev**: **CORE PROVEN this session, not yet wired.** A disposable VM joins the tailnet,
  borrows OMP auth from the broker over the private overlay, runs opus-5, has docker. The daemon still dispatches
  to **local git-worktrees** (`isolation.py` WorktreeIsolation); the exe.dev adapter is the next build.
- **Origin of truth → GitHub** (`github.com/cryptocrystian/factory`, `…/arxus`). Accepted merges push to origin.

## HOW TO REACH / OPERATE (over the tailnet — reliable, no VPN)
- **SSH the VPS:** `ssh -i ~/.ssh/factory_vps root@100.103.168.32` (raw :22 to the public IP is blocked from the
  workstation network — always use the tailnet IP). If the workstation ever drops off the tailnet: `sudo tailscale up`.
- **Daemon:** `systemctl status arxus-factory` · logs `journalctl -u arxus-factory -f` · `--max-parallel N`, `--poll`.
  Backlog `/root/factory/backlog.yml` (the daemon mutates it → the VPS copy is the LIVE queue and will show as git-dirty;
  that's expected, not drift). Escalation bundles in `runs/escalations/`.
- **Services (all `active`/`enabled`):** `arxus-factory` (daemon), `omp-auth-broker` (broker), `omp-broker-tailnet`
  (socat forwarder exposing the broker to exe.dev VMs on the tailnet only), `omp-broker-watchdog.timer` (alerts Buzz
  on credential failure), `tailscaled`. Broker health: `journalctl -u omp-auth-broker --since "$(systemctl show -p
  ActiveEnterTimestamp --value omp-auth-broker)" | grep -c 'credential disabled'` (0 = healthy).

---

## WHAT'S BUILT + LIVE (this session's deltas over the 08-13 spine)
1. **Durable auth (single-refresher broker).** Anthropic OAuth uses a *rotating* refresh token — two holders
   invalidate each other (the outage we hit). Fix: the VPS broker is the ONLY holder/refresher; everything borrows
   via `OMP_AUTH_BROKER_URL`/`_TOKEN` (`control-plane/omp.py:_child_env` loads `/root/.omp/broker-client.env`).
   Systemd + boot-persistent + watchdog. Proven: real opus-5 + gpt-5.6 borrow. Runbook `ops/AUTH-BROKER-RUNBOOK.md`.
2. **Durable access (Tailscale).** Raw :22 to the VPS is unusable from the workstation network (MTU/banner). Both
   boxes on a tailnet; always SSH the VPS at its 100.x. Same tailnet carries the exe.dev broker-borrow.
3. **GitHub is origin.** VPS `/root/factory` and `/root/projects/arxus` are real git clones (were hand-copied/stale).
   A GitHub token is provisioned on the VPS (`~/.git-credentials`; swap for a scoped PAT). `isolation.py` merge_back
   drops the node_modules symlink before staging so it can't be committed (fixed a real dirty-tree bug).
4. **Architect + PM governance IN THE LOOP.** `agents/architect/system.md` (+ `product-manager/system.md`); config
   roles (architect=Anthropic granted protected paths migrations+canon — ONLY role that is; PM=OpenAI, cross-family).
   `lanes/feature.py:_architect_resolve` runs when the builder can't converge (bounded `MAX_ARCH_ROUNDS=4`), authors
   the migration/canon, validates it via `gates.migration_gate` (`control-plane/validate_migrations.sh` — applies all
   migrations to a throwaway Postgres), hands app-logic back to the builder, escalates only true business decisions.
   **Guardrail (load-bearing):** resolve by making the build meet canon, NEVER weaken canon/tests to pass —
   `permissions.py` structurally bars the architect from editing tests; canon-lowering escalates.
5. **Continuous parallel daemon** (`orchestrator.py --daemon`): §9 allocate = WIP + decomposition gate
   (`canon.py:bindings`, `orchestrator._bindings/_overlaps`); parks escalations & keeps going; re-queues merge-race
   losers (I11) and **transient infra failures** (Anthropic `overloaded_error`) with backoff — only persistent
   failures escalate. `ops/arxus-factory.service`.
6. **Factory Brief console** (Artifact, plain-language status/decisions): https://claude.ai/code/artifact/dc2e9fcd-8fec-4140-910f-11b3d21f23bd
   (hand-populated snapshot; regenerate as state changes).

## ARXUS PRODUCT STATE
Repo `…/arxus` `main` = **898e9d5**. **Shipped (built + reviewer-accepted + merged):** canon (DEC-052→061),
schema migrations **0001→0010** (all apply clean), auth foundation, **JRN-S1** (valuation), **JRN-S3** (exit-readiness),
**JRN-S2** (published live listing — resolved autonomously by the architect: migrations 0007/0008/0009 + DEC-060),
**JRN-B3** (NDA→tiered docs — migrations 0004/0005/0006), **jurisdiction gate** (data-driven over the seeded set).
- **In flight now:** JRN-S4 (edit/version), JRN-B1 (buyer BQS) — building in parallel.
- **Not started (~15 journeys):** S5, B2, B4, T1/T2, N1/N2, M1, J1, F1, X1, BR1, G1/G2, AI1, P1 — the bulk of the product.
- **Detailed state lives in the repo:** `canon/Decision Log.md` (DEC-052→061), `Canonical Journeys v2.md`,
  `Acceptance Criteria v2.md`, `backlog.yml`.

## exe.dev — CORE PROVEN, ADAPTER NOT YET BUILT (Christian's paid account; the horizontal-scale substrate)
Proven end-to-end this session: a disposable exe.dev VM joins the tailnet, borrows OMP auth from the broker over
the private overlay, and runs a real opus-5 completion. The VM is a full machine (passwordless `sudo`, `docker`
group, `/dev/net/tun`). **The exact working recipe + gotchas are in the `factory-handoff.md` memory** (VPS-as-SSH-
jump-host since the workstation can't SSH exe VMs; `exedev` user + sudo; start `tailscaled`; mint a FRESH tailnet
auth key via the API key with `ephemeral:false`; scp `omp` from the VPS; broker at `http://100.103.168.32:9099`).
**Remaining:** (1) a working setup-script encoding the recipe so a VM self-runs a journey; (2) the exe.dev isolation
adapter (`acquire`=new VM, run lane, `merge_back`=push to GitHub, `destroy`=rm VM) behind the same Isolation port;
(3) wire the daemon to dispatch to VMs; (4) per-VM key + GitHub-token provisioning. Pair with the broker
**account-pool** (`OMP_AUTH_BROKER_ACCOUNT_POOL_FILE`) — one subscription rate-limits concurrency (~2 heavy opus-5
calls trigger `overloaded_error`; that's why N=2 today).

---

## OPEN DECISIONS (awaiting the human — non-blocking launch gates)
- **OPEN-JUR1** — one pre-launch legal green-light on the jurisdiction set as a whole (DEC-061). Seed populated
  (49 live / CA,NY defer_and_structure / 0 excluded), planning-grade until this sign-off. Not per-state.
- **OPEN-VAL1** — ratify the valuation benchmark table before real sellers (DEC-052).
- **OPEN-S3-1** — ratify the exit-readiness rubric before production (DEC-056).
These do NOT block building; they gate production exposure. The factory surfaces them; nothing stalls on them.

## NEXT PRIORITIES (agreed)
1. **exe.dev worker substrate** — setup-script + isolation adapter + daemon wiring + a full journey on a VM
   (core is de-risked; every piece works). Then broker account-pool for real concurrency.
2. **Wire Buzz as the decision plane** — escalations post to Buzz, Christian rules in Buzz, ruling → backlog, so he's
   never the go-between and Claude drops out of the loop (agent templates + the notifier fix). *(Notifier posting was
   flaky; the watchdog posts fine, so it's a routing/config fix.)*
3. **Canonicalize Disclosure & Consent** — the next foundation-corpus doc carrying enforceable data (like the State
   Regulatory Map → DEC-061). Needs Christian's research doc; Claude canonicalizes (seed + DEC), never fabricates.
4. **Product build-out** — let the parallel daemon drain the ~15 remaining journeys.

## GOTCHAS / STANDING CONSTRAINTS
- **Always SSH the VPS over the tailnet** (`root@100.103.168.32`), never the public IP. exe.dev VM access = via the
  VPS jump host (register a VPS-held key with `ssh-key add`).
- **Anthropic OAuth is rotating** — never let two machines hold the same creds. Broker = sole refresher.
- **The VPS backlog.yml is live runtime state** (daemon-mutated) → shows git-dirty on the VPS; that's expected.
- **One subscription rate-limits concurrency** — `overloaded_error` under parallel opus-5; daemon retries transient
  failures with backoff. Real scale = account-pool and/or exe.dev.
- **Migrations are a protected path** — only the architect (or human) authors them; the builder gets a remediation brief.
- **Auto-mode may block some VPS file-writes/secret-writes over SSH** — deploy via copy-paste blocks or a permission rule.
- **Rotate keys pasted in chat** (Tailscale API/auth keys, the broker token) when convenient.
- Standing: greenfield-only; workstation is a client not a worker; discipline (observable, gated, agent-proposes /
  code-disposes, architect-resolves-technical / human-rules-business) is sacred.
