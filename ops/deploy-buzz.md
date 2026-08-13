# Ops brief — Deploy Buzz on the Saipien VPS  (for the on-box Claude Code)

**You are Claude Code running on the Saipien Labs VPS** (Ubuntu 24.04, Docker 28 + Compose v2,
Traefik reverse proxy, n8n already running). A remote operator (Claude in the owner's WSL) is directing
this; it can't SSH here (the owner's network blocks outbound :22), so **you are the hands on this box.**

**Task:** deploy **Buzz** (https://github.com/block/buzz) — a self-hostable, agent-native collaboration
workspace (a Nostr relay + app where humans *and agents* are first-class members) — **behind the
existing Traefik**, with valid TLS, coexisting cleanly with n8n. Then verify and report.

**Why:** this VPS is the *persistent tier* of the Saipien software factory. Buzz becomes the human+agent
collaboration & notification layer — per-project channels where the factory posts escalations, the owner
and teammates (and agents) discuss and rule, and eventually GTM/marketing stakeholders collaborate.

## Ground truth you can rely on
- 2 vCPU · 7.8 GB RAM (~6.5 GB free) · 83 GB disk free — plenty for Buzz + n8n.
- **Traefik** (container `root-traefik-1`) owns :80/:443 with TLS. **n8n** runs privately on
  `127.0.0.1:5678` behind it. Docker + Compose v2 ready.

## Objective (definition of done)
Buzz reachable over **HTTPS with a valid cert**, its relay healthy, **n8n untouched and still working**,
and the remote operator able to reach the Buzz **API + `buzz-cli`** over 443 as a first-class agent member.

## Steps
1. **Discover before you change anything.** Find the Traefik/n8n compose project (try `docker compose ls`,
   `docker inspect root-traefik-1`, look under `/root` and `/opt`). Note: the Docker network Traefik uses,
   how it issues TLS (Let's Encrypt resolver? which entrypoints?), the **exact label pattern n8n uses**
   for its router/service, and **what hostname n8n is served on** (so Buzz doesn't collide).
2. **Choose Buzz's hostname** (see *Inputs* below). If a custom domain was provided (DNS already → this
   VPS), use `buzz.<domain>`. Otherwise use the Hostinger hostname `srv816212.hstgr.cloud` **only if n8n
   isn't already on that exact host**; if it is, stop and ask the operator rather than path-routing a
   full app.
3. **Use Buzz's official deploy bundle — follow their current docs, don't improvise the stack.** Consult
   the repo's `deploy/compose/` and README (Buzz is new; their instructions are authoritative). Clone to
   `/opt/buzz`. Expect a Compose stack: Postgres + Redis + MinIO + the Rust relay/app.
4. **Integrate with Traefik, not a second proxy.** Give Buzz's web/relay service Traefik labels that
   mirror n8n's pattern (same external network, same TLS resolver, the chosen host, the correct internal
   port). Do **not** bind Buzz to :80/:443 directly. Keep its Postgres/Redis/MinIO internal (no public
   ports).
5. **Secrets.** Generate strong values (DB password, MinIO keys, any relay signing keys) into
   `/opt/buzz/.env` (`chmod 600`). Follow Buzz's required env from their docs.
6. **Bring it up:** `docker compose up -d`; tail logs until the relay is healthy and migrations complete.
7. **Verify:** `curl -sI https://<buzz-host>/` shows a valid TLS cert; the Buzz web app loads; the relay
   endpoint responds; **n8n still works**; `docker ps` shows Buzz Up and Traefik + n8n unaffected.
8. **Enable the remote operator (the factory copilot) — this is a hard requirement.** Per Buzz's docs:
   confirm the REST/WebSocket API and `buzz-cli`; create an **agent keypair** for the operator as a
   first-class Buzz member plus an API/CLI token; make the API reachable on the same HTTPS host. Record
   where the keypair + token live.
9. **Report.** Write and print `/opt/buzz/DEPLOY-REPORT.md`: the Buzz URL, API/relay endpoint, where
   secrets + the operator keypair/token are stored (paths, **not values**), how to invoke `buzz-cli`, a
   `docker stats` footprint snapshot, confirmation n8n is intact, and any open items / unfinished Buzz
   features you had to skip.

## Inputs (from the operator)
- **Buzz hostname:** `buzz.saipienlabs.com` — Cloudflare DNS `A` record → `5.181.218.246` is set.
- **Cloudflare proxy / TLS — read carefully, this is the one thing that can break cert issuance:**
  The A record is currently **Proxied** (orange cloud). Cloudflare then terminates TLS at its edge and
  re-originates to this box, which can interfere with a Let's Encrypt **HTTP-01** challenge (the usual
  Traefik default). Handle it in this order:
  1. **First, discover how n8n does it** (Step 1). If n8n's host is *also* Cloudflare-proxied and its
     cert works, **mirror n8n's exact pattern** for Buzz — the working pattern is already proven on this
     box, so copy it.
  2. **Otherwise, deploy DNS-only-style first:** get Traefik to issue a real Let's Encrypt cert for
     `buzz.saipienlabs.com` with the record temporarily set to **DNS-only (grey cloud)** — ask the
     operator to flip it grey, confirm issuance, done. Then the operator re-enables **Proxied** and sets
     Cloudflare **SSL/TLS mode to Full (strict)** — the origin now has a valid LE cert, so strict
     validation passes and Cloudflare's protection + origin-hiding come back on.
  3. **Never** use Cloudflare **Flexible** SSL — it makes the Cloudflare→origin leg plain HTTP and causes
     redirect loops / an insecure origin.
  If issuance still fails under the proxy, the robust alternative is a Let's Encrypt **DNS-01** challenge
  (needs a Cloudflare API token) or a **Cloudflare Origin Certificate** installed on Traefik with SSL mode
  Full (strict). Note which path you took in the report; if you need the record flipped grey or an API
  token, stop and ask the operator rather than guessing.

## Guardrails
- **Additive only. Do not disrupt n8n or Traefik.** If a change would touch Traefik's config, back it up
  first and keep every n8n route intact.
- Buzz is early ("pending code" for some features). If a doc step is unclear or a feature is unfinished,
  deploy the working core, note the gap in the report, and don't force it.
- **No secret values** in logs or the report — reference their location only.
- If anything is ambiguous or risky, stop and surface it in the report rather than guessing.
