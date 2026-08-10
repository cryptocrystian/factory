# Foundation brief — Authentication (production-ready AuthProvider)

**Goal:** close **OPEN-S3-2** — provide a production-ready authentication provider **behind the existing
AuthProvider seam**, so authenticated journeys (JRN-S3 today; S2/S5/B1/B3 next) work in production, not
just via the dev-header actor. This is a **cross-cutting foundation**, not a single user journey.

## The seam already exists — build behind it, don't replace it

JRN-S3 shipped `lib/auth/actor.ts` with an **`AuthProvider` interface** and one provider, `dev-header`
(`production_ready = false`, reads `x-arxus-actor`, refuses production). That seam is the DEC-056
contract and is the same shape as the DEC-055 Mailer seam. **Do not break the dev-header provider** —
tests and local/dev depend on it. Add a *second* provider and make it selectable.

## What to build

1. **A `supabase` AuthProvider** behind the same interface:
   - Resolves the acting user from a **real Supabase Auth session** (server-side; verify the session /
     JWT via `@supabase/supabase-js` using the project URL + keys already in `.env.example`) and returns
     the `app_user` id (the ownership-chain identity RLS expects).
   - Declares `production_ready = true`.
   - **No anonymous fall-through in any environment.** An unauthenticated request is refused (401), never
     silently allowed.
2. **Provider selection** via `AUTH_PROVIDER` (already in `.env.example`): `dev-header` (non-prod default)
   or `supabase`. When `AUTH_PROVIDER=supabase` and a valid session is present, `app/api/assessment` (and
   any actor-gated route) resolves the seller **without the 503** the dev-header path returns in prod.
3. **A minimal sign-in surface** sufficient to establish a session — a `/sign-in` route using Supabase
   Auth (email/OTP or password per DEC-004) and a session cookie/handler. Keep it foundational: this is
   the auth *plumbing*, not full account management (profiles, org management, MFA step-up are later work
   — leave clean seams, note them, don't build them here).

## Canon that governs this

- **DEC-004** — Supabase Auth at launch (TOTP/phone/WebAuthn MFA integrated with RLS for step-up on
  sensitive rows). MFA/step-up is noted for later; the core here is session → actor.
- **DEC-056** — auth is resolved through the AuthProvider seam; production refuses until a production-ready
  provider is configured. This foundation makes `supabase` that provider.
- RLS ownership-chain pattern (`app.current_user_id()`) — the resolved actor id must be the one RLS keys
  off. Do not change the schema/migrations.
- Secrets discipline (DEC-002/003): keys come from env, never committed; service-role stays server-only.

## Scope & testability (important — don't escalate on this)

There is **no live Supabase project wired locally** (no Docker), so integration against a real Supabase
session is a **launch gate**, exactly like the rubric/benchmark/email gates. Build the provider so its
logic is **unit-testable without a live Supabase**: inject/verify the session through a small verifier
interface so tests can exercise (a) `supabase` provider maps a valid session → the right `app_user`,
(b) an absent/invalid session → 401, (c) `AUTH_PROVIDER` selection picks the right provider, (d) the
dev-header provider still works and still refuses production. The real network verification sits behind
the interface and is exercised at launch. **Build-acceptance = L0 green + these unit tests green.** State
the live-Supabase integration as the remaining launch gate; do not treat it as a failure.

## Acceptance

- `AUTH_PROVIDER=supabase` selects a `production_ready=true` provider; a valid session resolves the actor
  and the actor-gated routes no longer 503 in production.
- No anonymous fall-through anywhere; unauthenticated → 401.
- JRN-S3 unchanged: the dev-header provider still works in dev/test and still refuses production.
- L0 (types/tokens) green; unit tests cover the four cases above. Source only — no schema/canon changes.
