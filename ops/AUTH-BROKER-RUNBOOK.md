# Auth-broker — durability runbook

**Goal: the factory's model auth stays up on its own. Re-login is a rare, *notified* event — never a
constant chore, never a mid-run surprise.** This is what makes the VPS (not Christian's PC) the brain.

---

## Why auth broke before (root cause, 2026-08-13)

Anthropic's Claude Pro/Max OAuth uses a **rotating refresh token**: every refresh mints a new refresh
token and **invalidates the previous one**. The factory was bootstrapped by *copying* `~/.omp` to the
VPS, so the workstation and the VPS each held **the same** refresh token. The first machine to refresh
silently invalidated the other — and eventually itself. **Two independent holders of one rotating
credential is a guaranteed, recurring outage.** (openai-codex tolerated this; Anthropic did not.)

## The fix: single-refresher discipline

Exactly one process — **the auth-broker on the VPS** — holds and rotates the OAuth. Everything else
(exe.dev workers, the on-box factory, even interactive shells) **borrows** a short-lived credential
from the broker over HTTP and never refreshes the raw OAuth. One refresher ⇒ no rotation race ⇒ the
credential stays alive as long as the broker keeps running. The broker is a durable systemd service,
so "keeps running" survives crashes and reboots.

```
                          ┌───────────────────────────────┐
   exe.dev worker  ──┐    │  VPS (persistent brain)        │
   exe.dev worker  ──┼──▶ │  omp-auth-broker.service       │  ← ONLY holder/refresher of the OAuth
   on-box factory  ──┘    │   127.0.0.1:9099 (bearer-gated)│
   (all set OMP_AUTH_      │  systemd: Restart=always,      │
    BROKER_URL/TOKEN)      │  enabled (boot-persistent)     │
                          └───────────────────────────────┘
```

---

## Deployed state (as of 2026-08-13)

- ✅ **Anthropic re-authed** on the VPS broker; `openai-codex` healthy. `credential disabled` count = 0.
- ✅ **`omp-auth-broker.service`** installed, **active + enabled** (starts on boot, `Restart=always`),
  bound to `127.0.0.1:9099`. Unit is version-controlled at `ops/omp-auth-broker.service`.
- ✅ **Bearer token enforced** — the borrow path returns `unauthorized` without the token
  (`/root/.omp/auth-broker.token`, mode 0600). `status` is an unauthenticated reachability ping only.
- ✅ **Factory code enforces borrowing** — `control-plane/omp.py:_child_env()` loads
  `OMP_AUTH_BROKER_URL`/`_TOKEN` from a broker-env file into every spawned omp, fail-open if absent.

### Still to deploy on the VPS (see **Deploy block** below)
- ⬜ `/root/.omp/broker-client.env` (the client discipline file the factory + shells read).
- ⬜ The **watchdog timer** (`ops/omp-broker-watchdog.*` + `ops/broker-watchdog.sh`) that alerts Buzz
  on any credential failure.

> Auto-mode blocks Claude from writing files to the VPS over SSH (an intentional guardrail for a
> remote root box), so these two steps are a copy-paste block **you** run. Read-only checks and
> `systemctl` were allowed, which is how the broker service is already live.

---

## Deploy block (run once, on the VPS as root)

Self-contained — does **not** depend on `/root/factory` being in sync (only `buzz.py`, already current).

```bash
set -euo pipefail

# 1) Client discipline env. The token never leaves the box. Factory code + shells read this file;
#    with it in place, nothing but the broker ever refreshes the OAuth.
umask 077
printf 'OMP_AUTH_BROKER_URL=http://127.0.0.1:9099\nOMP_AUTH_BROKER_TOKEN=%s\n' \
  "$(cat /root/.omp/auth-broker.token)" > /root/.omp/broker-client.env
chmod 600 /root/.omp/broker-client.env

# 2) On-box shells borrow too (belt-and-suspenders; the factory already loads the file in code).
grep -q broker-client.env /root/.bashrc 2>/dev/null || \
  printf '\n# Factory: borrow OAuth from local auth-broker (single-refresher discipline)\n[ -f /root/.omp/broker-client.env ] && set -a && . /root/.omp/broker-client.env && set +a\n' >> /root/.bashrc

# 3) Install the watchdog script to a stable path.
install -m 0755 /root/factory/ops/broker-watchdog.sh /usr/local/bin/omp-broker-watchdog 2>/dev/null \
  || { echo "NOTE: /root/factory/ops/broker-watchdog.sh not found (stale factory). Sync it, or paste the script from ops/broker-watchdog.sh."; }

# 4) Install + start the watchdog timer.
install -m 0644 /root/factory/ops/omp-broker-watchdog.service /etc/systemd/system/omp-broker-watchdog.service
install -m 0644 /root/factory/ops/omp-broker-watchdog.timer   /etc/systemd/system/omp-broker-watchdog.timer
systemctl daemon-reload
systemctl enable --now omp-broker-watchdog.timer

# 5) Sanity.
systemctl is-active  omp-auth-broker.service          # -> active
systemctl is-enabled omp-auth-broker.service          # -> enabled
systemctl list-timers omp-broker-watchdog.timer --no-pager
/usr/local/bin/omp-broker-watchdog                    # -> logs state=HEALTHY
```

---

## Health check (any time)

```bash
# Is the broker up, enabled, and are all credentials live?
systemctl is-active omp-auth-broker.service
journalctl -u omp-auth-broker.service \
  --since "$(systemctl show -p ActiveEnterTimestamp --value omp-auth-broker.service)" \
  --no-pager | grep -c 'credential disabled'          # -> 0 means healthy
```

`0` disabled = healthy. Any non-zero = a family needs re-auth (see below). The watchdog posts the same
finding to Buzz **#factory-escalations** automatically.

---

## Re-auth (the rare manual event)

Needed only if a credential goes `disabled` — i.e. the subscription session was revoked (password
change, Anthropic-side logout, or a very long broker downtime). With the single-refresher discipline
in place this should essentially never happen on its own.

**Requires a browser (Christian).** From the workstation, with the VPN up (SSH to the VPS working):

```bash
# Runs the OAuth browser flow locally and installs the result into the VPS broker over SSH.
omp auth-broker login anthropic --via=root@5.181.218.246
# then restart so the broker reloads the refreshed credential, and confirm:
ssh -i ~/.ssh/factory_vps root@5.181.218.246 'systemctl restart omp-auth-broker && sleep 4 && \
  journalctl -u omp-auth-broker --since "$(systemctl show -p ActiveEnterTimestamp --value omp-auth-broker)" \
  --no-pager | grep -c "credential disabled"'   # -> 0
```

`openai-codex` re-auth is the same with `openai-codex` (or `openai-codex-device` for a headless code flow).

---

## Later: exposing the broker to exe.dev workers (Priority #2, exposure step)

Workers run on exe.dev (public internet); SSH-22 into the VPS is blocked and there's no tailnet, so
workers reach the broker over **HTTPS via the existing Traefik** (`root-traefik-1`, already terminating
TLS at :443 for Buzz). Before exposing a credential-vending service publicly, this must be true:

1. **Verify the broker vends short-lived tokens, not the raw OAuth.** Confirm a borrow returns a
   short-lived access credential (the `OMP_AUTH_BROKER_SNAPSHOT_*` cache path suggests it does) so a
   compromised disposable worker can never steal or rotate the master refresh token. **Do this first.**
2. Traefik router on a dedicated host (e.g. `broker.saipienlabs.com`) → `127.0.0.1:9099`, TLS on.
3. Keep the **bearer token** the only credential; rotate with `omp auth-broker token --regenerate`
   (then update `/root/.omp/broker-client.env` and every worker's env). Treat it like a root secret —
   env only, never in git.
4. Restrict exposure as far as practical (IP allow-list to exe.dev egress if stable; rate-limit).
5. Consider `OMP_AUTH_BROKER_ACCOUNT_POOL_FILE` to pool multiple subscription accounts once run
   volume rises (spreads rate limits across accounts).

Until exposure is done, the broker stays localhost-only and only on-box runs can borrow.
