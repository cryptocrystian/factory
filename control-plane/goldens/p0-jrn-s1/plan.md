# Implementation Plan — JRN-S1 · Visitor completes a valuation and receives an emailed result

**Run:** 2026-08-07-p0-jrn-s1 · **Repo:** `~/projects/arxus` (greenfield: canon + schema, no app yet)
**Stack (locked):** Next.js (App Router, TypeScript) + Supabase — DEC-001/004/005
**Governing canon:** JRN-S1 (Journeys v2) · AC-S1-T/01/02/03/04/N1/N2 + AC-X-07/16 (Acceptance Criteria v2) ·
DEC-014, DEC-015, DEC-019, DEC-041, DEC-042, DEC-051 · INV-010, INV-012 · HARD-003, HARD-015, HARD-016

---

## 0. Scope

**In scope**

1. Stand up the Next.js + Supabase application scaffold on top of the existing commit (canon + `0001_init.sql`).
2. One public, unauthenticated surface: `/valuation` — a form capturing the six canon inputs + `lead_email`.
3. One server endpoint: `POST /api/valuation` — validate → compute → persist `valuation` → dispatch email →
   record `valuation_created`.
4. A deterministic, pure valuation engine behind a source-tagged benchmark seam (DEC-019).
5. A result surface (inline after submit + a permalink `/valuation/[id]`) showing range, comps, SF scenarios,
   source tags, and a next-step CTA.
6. A stubbed mailer (the one external effect) with a file outbox so the terminal is verifiable offline.
7. The test surface: unit, integration (local Supabase), E2E (browser), and the L0 mechanical token check.

**Explicitly out of scope** (not required by AC-S1-*, do not build)

- Any schema change. `0001_init.sql` and `canon/` are read-only inputs. No new migration, no new column,
  no new DB function or trigger.
- Auth, sessions, accounts (AC-S1-01: "no auth required").
- Conversion of a Valuation to a Business (`business_id`, `converted_to_listing` stay at their defaults —
  INV-010 is satisfied by never writing `business_id`). That is JRN-S2.
- A real email provider (context: stub/log at P0). Rate limiting, captcha, analytics beacons — not governed
  by canon for this journey, and a beacon would jeopardise AC-S1-N2.

**Non-negotiables carried into the design**

| Constraint | How this plan honours it |
|---|---|
| DEC-014 (outputs derived) | `outputs` is computed server-side from `inputs` only. The endpoint never accepts, merges, or trusts a client-supplied `outputs`. |
| DEC-019 / AC-S1-03 | All benchmark numbers come from one `BenchmarkProvider` interface; the only P0 implementation tags every value `source: "industry_benchmark"`. |
| INV-012 | Exactly one `activity_event` row per created `valuation`. Written once, never updated (DB trigger `ae_append_only` enforces). |
| RLS / service role | `valuation` + `activity_event` are service-role-written (migration note, lines 682–686). All writes happen in a server-only Supabase client with the service key. No browser Supabase client ships. |
| AC-X-07 / AC-X-16 / HARD-015/016 | Zero raw colour literals in app code; all colour comes from CSS custom properties generated from `canon/arxus-design-tokens.js`. A mechanical check fails the build on a raw hex. |
| HARD-003 | SF scenarios are illustrative structures **between the parties**; the surface and the email carry a non-muted neutrality line and never present Arxus as lender/arranger. |
| Determinism | The engine is a pure function `(inputs, benchmark) -> outputs` with no `Date.now()`, no RNG, no I/O inside. Timestamps are injected by the caller. |

---

## 1. Files & structure

Everything below is **new**. Nothing under `canon/` or `supabase/migrations/` is modified.

```
arxus/
├─ package.json                          # next, react, @supabase/supabase-js, zod, tailwindcss,
│                                        # vitest, @playwright/test, @axe-core/playwright
├─ tsconfig.json                         # strict: true; paths: "@/*" -> "./*"
├─ next.config.mjs
├─ tailwind.config.ts                    # theme = tailwindTheme imported from the canon token file
├─ postcss.config.mjs
├─ .env.example                          # documented below (§7)
├─ .gitignore                            # += .env.local, .mail-outbox/, test-results/
│
├─ scripts/
│  ├─ generate-tokens-css.mjs            # imports canon/arxus-design-tokens.js -> app/tokens.generated.css
│  └─ check-tokens.mjs                   # L0 mechanical check (AC-X-07 / AC-X-16)
│
├─ app/
│  ├─ tokens.generated.css               # GENERATED, git-ignored; produced by prebuild/predev
│  ├─ globals.css                        # @import tokens.generated.css; @tailwind directives only
│  ├─ layout.tsx                         # <html data-theme>, font links, body uses token vars
│  ├─ page.tsx                           # minimal index -> links to /valuation
│  ├─ valuation/
│  │  ├─ page.tsx                        # server component; renders <ValuationForm/>
│  │  └─ [id]/page.tsx                   # permalink result page (service-role read by uuid)
│  └─ api/valuation/route.ts             # POST handler; runtime='nodejs'; dynamic='force-dynamic'
│
├─ components/
│  ├─ ui/{Button,Card,Field,TextInput,Select,Callout}.tsx   # token-class-only primitives
│  └─ valuation/
│     ├─ ValuationForm.tsx               # 'use client'; two client-side steps, ONE submit
│     ├─ ValuationResult.tsx             # range + comps + SF scenarios + source tags + CTA
│     └─ SourceTag.tsx                   # renders `source` provenance chip
│
├─ lib/
│  ├─ supabase/server.ts                 # import 'server-only'; createClient(url, SERVICE_ROLE_KEY,
│  │                                     #   {auth:{persistSession:false, autoRefreshToken:false}})
│  ├─ valuation/
│  │  ├─ types.ts                        # ValuationInputs, ValuationOutputs, Benchmark, Comp, SfScenario
│  │  ├─ input-schema.ts                 # zod schema + numericString helper (AC-S1-01, AC-S1-N1)
│  │  ├─ benchmarks.ts                   # the seed benchmark table (source-tagged, as_of dated)
│  │  ├─ provider.ts                     # BenchmarkProvider interface + IndustryBenchmarkProvider
│  │  ├─ compute.ts                      # PURE computeValuation() (AC-S1-02, AC-S1-03)
│  │  ├─ format.ts                       # currency/percent formatters (shared by UI + email)
│  │  └─ service.ts                      # orchestration: persist + email + event (AC-S1-T)
│  ├─ events/activity.ts                 # recordActivityEvent()
│  ├─ email/
│  │  ├─ mailer.ts                       # Mailer interface + LogMailer + getMailer()
│  │  └─ templates/valuation-result.ts   # subject/html/text (AC-S1-04)
│  └─ constants.ts                       # NEXT_STEP_CTA (single place; see OQ-2)
│
└─ tests/
   ├─ unit/{compute,input-schema,email-template,benchmarks}.spec.ts
   ├─ integration/valuation-endpoint.spec.ts        # needs local Supabase
   └─ e2e/valuation.spec.ts                         # Playwright; needs running app
```

---

## 2. Ordered build steps

Each step names its criteria. Do them in order; each ends in a runnable state.

### Step 1 — Scaffold (enables everything; no criterion of its own)
Next.js App Router + TypeScript (`strict`), Tailwind, Vitest, Playwright. `tsconfig` path alias `@/*`.
`package.json` scripts:
`dev` (runs `predev` → token CSS), `build` (`prebuild` → token CSS), `test:unit`, `test:int`, `test:e2e`,
`check:tokens`, and `check` = `check:tokens && test:unit`.

### Step 2 — Design-token pipeline → satisfies AC-X-07, AC-X-16, HARD-015/016
- `scripts/generate-tokens-css.mjs`: dynamic-imports `canon/arxus-design-tokens.js`, writes
  `app/tokens.generated.css` containing `baseCSSReset` (which already emits `:root`, `[data-theme="dark"]`,
  and the `prefers-color-scheme` block). Generated, never hand-edited, git-ignored — so the tokens can only
  come from canon.
- `tailwind.config.ts` imports `tailwindTheme`, `spacing`, `radii` from the same canon file and spreads them
  into `theme.extend`. Spacing scale is `{xs:4,sm:8,md:12,lg:16,xl:24,2xl:32,3xl:48,4xl:72}` — components use
  only these steps.
- `scripts/check-tokens.mjs`: scans `app/**`, `components/**`, `lib/**` (excluding `tokens.generated.css`) and
  **fails** on: any `#rgb`/`#rrggbb`/`#rrggbbaa` literal, any `rgb(`/`rgba(`/`hsl(` literal, the strings
  `#fff`/`#ffffff`/`#000`/`#000000` in any case, and any Tailwind arbitrary value `[...px]` / `[#...]`.
  Exit non-zero on any hit.
- Type floor: body/base ≥ 14px, the smallest text used on this consumer surface ≥ 13px
  (`constraints.minConsumerFontPx`). The seller-financing neutrality line and the email/data notice are
  rendered at `body` size in `--text` (never `--text-muted`) — `disclosureNeverMuted`.

### Step 3 — Supabase wiring (no criterion; enables T/N1)
- `lib/supabase/server.ts` — `import 'server-only'` at the top so a client bundle importing it is a build
  error. Uses `SUPABASE_SERVICE_ROLE_KEY`. This is the **only** Supabase client in the codebase.
- Local dev: `supabase start` + `supabase db reset` applies `supabase/migrations/0001_init.sql` unchanged.
- Sanity check before proceeding: service-role `insert into valuation` succeeds and an anon-key insert is
  denied (RLS default-deny). Do not add policies.

### Step 4 — Input contract → AC-S1-01, AC-S1-N1
`lib/valuation/input-schema.ts`, zod. The form posts **strings**; the schema does the numeric proof so
`"abc"` and `""` fail rather than coercing.

```
numericString(opts) = z.string().trim().min(1, "Required")
    .regex(opts.allowNegative ? /^-?\d+(\.\d{1,2})?$/ : /^\d+(\.\d{1,2})?$/, "Enter a number")
    .transform(Number)
    .refine(Number.isFinite, "Enter a number")
```

| Field (canon: AC-S1-01) | Key | Rule | On failure |
|---|---|---|---|
| industry | `industry` | `z.enum(INDUSTRY_KEYS)` (§4.1) | "Select an industry" |
| revenue | `revenue_ttm` | `numericString()` > 0, ≤ 1e12 | "Enter a number" / "Revenue must be greater than 0" |
| EBITDA/SDE | `ebitda_sde` | `numericString({allowNegative:true})` | "Enter a number" |
| years | `years_in_business` | `numericString()` integer, 0…150 | "Enter a whole number of years" |
| location | `location_state` | `z.enum(US_STATE_CODES)` (50 + DC) | "Select a state" |
| " (optional) | `metro` | `z.string().trim().max(80).optional()` | — |
| owner involvement | `owner_involvement` | `z.enum(['absentee','part_time','full_time','owner_dependent'])` (OQ-3) | "Select one" |
| email capture | `lead_email` | `z.string().trim().toLowerCase().email()` | "Enter a valid email address" |

Also: `ebitda_sde` ≤ `revenue_ttm` → error "SDE/EBITDA cannot exceed revenue" (a real field validation, still
AC-S1-N1 shaped). Reject unknown keys (`.strict()`), reject any `outputs` key outright (DEC-014).
Persisted `inputs` = the parsed object **minus `lead_email`**, plus `schema_version: 1` (email lives in the
`lead_email` column, which is `citext`).

### Step 5 — Benchmark seam → AC-S1-03, DEC-019
`provider.ts`:
```ts
export interface BenchmarkProvider {
  readonly source: 'industry_benchmark' | 'proprietary';
  get(industry: IndustryKey): Benchmark;   // synchronous, total (falls back to 'other')
}
```
`IndustryBenchmarkProvider` reads `benchmarks.ts` and stamps `source: 'industry_benchmark'` and
`as_of: '2026-01-01'` on every returned object. A later proprietary provider swaps here and nowhere else.
**Rule the builder must not break:** `compute.ts` never imports `benchmarks.ts` directly — only the provider.

### Step 6 — Compute engine → AC-S1-02, AC-S1-03 (§4 gives the exact arithmetic)
`computeValuation(inputs, benchmark, meta) → ValuationOutputs`. Pure: no I/O, no clock, no randomness;
`meta = { computed_at, engine_version }` is passed in.

### Step 7 — Mailer + template → AC-S1-04, external-effect isolation (§6)
### Step 8 — Endpoint + service orchestration → AC-S1-T, AC-S1-N1 (§3 gives the exact order)
### Step 9 — UI: form, result, permalink → AC-S1-01, AC-S1-02 (display), AC-S1-04 (CTA parity), AC-S1-N2
### Step 10 — Tests + checks → §5

---

## 3. Data flow

```
Browser (/valuation, no auth, no Supabase client)
  │  step 1 fields → step 2 (email)   [client state only — NOTHING persisted, NO network call]
  │  single submit
  ▼
POST /api/valuation   { industry, revenue_ttm, ebitda_sde, years_in_business,
                        location_state, metro?, owner_involvement, lead_email }   (all strings)
  │
  ├─ 1. schema.safeParse(body)
  │       └─ FAIL → 422 { ok:false, errors:{ field: message } }   ⟵ AC-S1-N1: return BEFORE any DB call
  │
  ├─ 2. benchmark = provider.get(inputs.industry)                  ⟵ DEC-019
  ├─ 3. outputs   = computeValuation(inputs, benchmark, meta)      ⟵ pure, DEC-014
  │       └─ assertOutputsValid(outputs) — throws 500 if low>high | comps<1 | sf<1 | any missing source
  │
  ├─ 4. INSERT valuation { inputs, outputs, lead_email }
  │       (business_id NULL, converted_to_listing default false → INV-010 trivially holds)
  │       .select('id, created_at').single()
  │
  ├─ 5. mailer.send(renderValuationResultEmail({ id, outputs, lead_email }))
  │       → { status:'sent'|'failed', message_id, provider }      ⟵ the only external effect
  │
  ├─ 6. INSERT activity_event {                                    ⟵ INV-012: exactly one
  │        event_type:'valuation_created',
  │        subject_entity_type:'valuation', subject_entity_id:<valuation.id>,
  │        actor_user_id: null,                                    ⟵ public/anonymous
  │        payload: { industry, value_range:{low,high},
  │                   benchmark_source:'industry_benchmark',
  │                   engine_version, email:{ dispatched:bool, message_id, provider } }
  │        occurred_at: default now()
  │      }
  │       └─ FAIL → compensating DELETE of the valuation row, then 500.
  │          (Rationale: the terminal is "record + event"; a record with no event would be a
  │           silent INV-012 breach. activity_event is append-only so the compensation must run
  │           on the valuation side. Never the reverse — never delete an event.)
  │
  └─ 7. 200 { ok:true, id, outputs, email:{ dispatched }, permalink:'/valuation/<id>' }
        (if step 5 reported 'failed': still 200 with email.dispatched=false — the event records the
         truth; the client shows "we couldn't email your copy" plus the on-screen result. Do NOT
         roll back: the Valuation and its event are both true statements about what happened.)
  ▼
Client renders <ValuationResult/> inline + link to the permalink.
```

**Ordering rationale (write it down, don't reshuffle):** email is dispatched *before* the event is written so
the event payload can carry the dispatch outcome — the event is then the single durable proof of the JRN-S1
terminal (record + outputs + email). The valuation row must exist first because the email and the event both
reference its id.

**Permalink read (`/valuation/[id]`):** server component, `lib/supabase/server.ts`, `select … where id = $1`.
The uuid is unguessable and is only sent to `lead_email`; no listing/enumeration route is exposed. Unknown id
→ `notFound()`.

---

## 4. Compute logic (exact — the builder implements this literally)

### 4.1 Benchmark table (`benchmarks.ts`)
`industry_vertical` is free text (NAICS/SIC) in the schema, so the form uses a curated NAICS-sector-aligned
key set that maps cleanly to `business.industry_vertical` at conversion time. Every row carries
`source:'industry_benchmark'`, `as_of:'2026-01-01'`, and ≥ 2 comp archetypes. **These multiples are seed
placeholders behind the DEC-019 seam — see OQ-4.**

| key | label | SDE mult low/base/high | Revenue mult low/base/high |
|---|---|---|---|
| `home_services` | Home & Trade Services (NAICS 23, 56) | 2.0 / 2.6 / 3.2 | 0.45 / 0.60 / 0.80 |
| `food_beverage` | Restaurants & Food Service (72) | 1.6 / 2.0 / 2.6 | 0.30 / 0.40 / 0.55 |
| `retail` | Retail Trade (44–45) | 2.0 / 2.5 / 3.0 | 0.35 / 0.45 / 0.60 |
| `healthcare` | Health Care Services (62) | 2.8 / 3.5 / 4.5 | 0.60 / 0.85 / 1.20 |
| `professional_services` | Professional & Business Services (54) | 2.5 / 3.2 / 4.0 | 0.70 / 0.95 / 1.30 |
| `manufacturing` | Manufacturing (31–33) | 3.0 / 3.8 / 4.8 | 0.55 / 0.75 / 1.00 |
| `transport_logistics` | Transportation & Warehousing (48–49) | 2.4 / 3.0 / 3.8 | 0.40 / 0.55 / 0.75 |
| `technology` | Information & Software (51) | 3.5 / 4.5 / 6.0 | 1.20 / 1.80 / 2.60 |
| `other` | Other / Mixed *(fallback)* | 2.0 / 2.6 / 3.4 | 0.40 / 0.55 / 0.75 |

Comp archetypes per row (2 each), e.g. for `home_services`:
`{ label:'HVAC & plumbing, owner-managed', revenue_band:'$500K–$2M', sde_margin_pct:18, multiple_applied:2.4 }`
`{ label:'Multi-crew trade services, GM in place', revenue_band:'$2M–$10M', sde_margin_pct:14, multiple_applied:3.0 }`

### 4.2 Basis selection
```
if ebitda_sde > 0 → basis='ebitda_sde', metric=ebitda_sde, mult=benchmark.sde_multiple
else              → basis='revenue',    metric=revenue_ttm, mult=benchmark.revenue_multiple
```
Record `outputs.method = { basis, reason }` (`reason:'non_positive_earnings_fallback'` in the else branch).

### 4.3 Adjustment factors (multiplicative)
| Driver | Bucket → factor |
|---|---|
| `years_in_business` | `<2` → 0.90 · `2–4` → 0.95 · `5–9` → 1.00 · `≥10` → 1.05 |
| `owner_involvement` | `absentee` → 1.08 · `part_time` → 1.03 · `full_time` → 0.95 · `owner_dependent` → 0.88 |
| scale (`revenue_ttm`) | `<500_000` → 0.92 · `<2_000_000` → 1.00 · `<10_000_000` → 1.06 · else → 1.10 |

`adjustment = clamp(round(f_years * f_owner * f_scale, 4), 0.75, 1.30)`
Expose `outputs.adjustments = [{driver, bucket, factor}, …]` and `outputs.adjustment_total`.

### 4.4 Value range → **AC-S1-02**
```
raw_low  = metric * mult.low  * adjustment
raw_mid  = metric * mult.base * adjustment
raw_high = metric * mult.high * adjustment
round1k(x) = Math.round(x / 1000) * 1000
low  = Math.max(0, round1k(raw_low))
high = Math.max(low, round1k(raw_high))     // hard guarantee low ≤ high after rounding
mid  = Math.min(high, Math.max(low, round1k(raw_mid)))
outputs.value_range = { low, high, midpoint: mid, currency:'USD',
                        basis, multiple_low: mult.low, multiple_high: mult.high,
                        source: 'industry_benchmark', as_of }
```

### 4.5 Comps → **AC-S1-02 (≥1), AC-S1-03**
For each archetype in `benchmark.comps` (guaranteed ≥ 2):
```
{ label, industry: benchmark.label, revenue_band, sde_margin_pct,
  multiple_applied, basis,
  implied_value: round1k(metric * multiple_applied),   // unadjusted — comparable, not the estimate
  source: 'industry_benchmark', as_of }
```
Order is the table order (deterministic). A unit test asserts every benchmark row has ≥ 1 archetype.

### 4.6 Seller-financing scenarios → **AC-S1-02 (≥1)**
Three fixed structures applied to `value_range.midpoint` (`P0 = midpoint`):

| key | label | down % | note % | APR | term |
|---|---|---|---|---|---|
| `standard` | Standard seller note | 20 | 80 | 8.0 | 60 mo |
| `moderate` | Longer term, larger down | 35 | 65 | 7.5 | 84 mo |
| `conservative` | Half cash, extended term | 50 | 50 | 7.0 | 120 mo |

```
down_payment   = round1k(P0 * down_pct/100)
note_principal = P0 - down_payment
r = apr/100/12
monthly_payment = round2(note_principal * r / (1 - Math.pow(1+r, -term_months)))
total_interest  = round2(monthly_payment * term_months - note_principal)
```
Each scenario object also carries `source:'industry_benchmark'`, `as_of`, and
`disclaimer_key:'no_platform_financing'`. If `P0 === 0` (degenerate input), emit the three scenarios with zero
amounts — never an empty array (AC-S1-02).

### 4.7 Output envelope
```jsonc
{
  "schema_version": 1,
  "engine_version": "valuation@1.0.0",
  "computed_at": "<ISO>",
  "method": { "basis": "ebitda_sde", "reason": null },
  "benchmark": { "provider": "industry_benchmark", "industry": "home_services",
                 "source": "industry_benchmark", "as_of": "2026-01-01" },
  "adjustments": [ … ], "adjustment_total": 1.0403,
  "value_range":  { …, "source": "industry_benchmark" },
  "comps":        [ { …, "source": "industry_benchmark" } ],
  "sf_scenarios": [ { …, "source": "industry_benchmark" } ],
  "disclaimers": { "estimate": "…", "no_platform_financing": "…" }
}
```
`assertOutputsValid()` (called by the service, exported for tests): `low ≤ high`, `comps.length ≥ 1`,
`sf_scenarios.length ≥ 1`, and a recursive walk asserting every object that carries a benchmark-derived
number carries a non-empty `source`.

---

## 5. Verification — criterion → check

| Criterion | Check | Where | Needs |
|---|---|---|---|
| **AC-S1-T** (terminal) | POST valid payload → 200; then assert **(a)** one `valuation` row with `outputs.value_range/comps/sf_scenarios` present and `lead_email` = submitted address, **(b)** exactly one `activity_event` with `event_type='valuation_created'`, `subject_entity_type='valuation'`, `subject_entity_id=<id>`, `actor_user_id IS NULL`, **(c)** one outbox file whose `to` = `lead_email`. | `tests/integration/valuation-endpoint.spec.ts` | local Supabase + outbox dir |
| **AC-S1-T** (browser) | Fill the form as an anonymous visitor with no cookies → submit → result visible → same three DB/outbox assertions. | `tests/e2e/valuation.spec.ts` | running app + local Supabase |
| **AC-S1-01** | Render `/valuation`; assert an accessible control exists for each of: industry, revenue, EBITDA/SDE, years, location(state), owner involvement, email. Assert the page returns 200 with **no** auth cookie/header and no redirect to a sign-in route. | E2E | running app |
| **AC-S1-02** | Table-driven unit test over **every** industry key × a matrix of inputs (positive SDE, zero SDE, negative SDE, 0 years, 40 years, each owner-involvement value, revenue at each scale boundary): `value_range.low ≤ value_range.high`, `comps.length ≥ 1`, `sf_scenarios.length ≥ 1`. Plus the rounding edge case where `raw_low ≈ raw_high`. | `tests/unit/compute.spec.ts` | none |
| **AC-S1-02** (displayed) | Result surface shows a low and a high with low ≤ high, ≥1 comp row, ≥1 SF scenario card. | E2E | running app |
| **AC-S1-03** | Recursive walk of the computed `outputs` across all industries: every node carrying a benchmark-derived value has `source === 'industry_benchmark'`; no node has a missing/empty/other `source`. Second test: `benchmarks.ts` — every row and every comp archetype is source-tagged and `as_of`-dated. Third: `IndustryBenchmarkProvider.source === 'industry_benchmark'`. | `tests/unit/{compute,benchmarks}.spec.ts` | none |
| **AC-S1-03** (visible) | Result surface renders a `SourceTag` ("Industry benchmark · as of Jan 2026") on the range block, the comps block, and the SF block. | E2E | running app |
| **AC-S1-04** | Render the email template for a known output: assert the text **and** html bodies contain the formatted low and high (`$X,XXX,XXX`) and a CTA anchor whose href === the permalink built from `NEXT_PUBLIC_SITE_URL` and whose label is the next-step label. Assert the plain-text part is non-empty. | `tests/unit/email-template.spec.ts` | none |
| **AC-S1-04** (end-to-end) | After a real submit, read the outbox JSON and assert the same two things on the actual dispatched message. | Integration | local Supabase |
| **AC-S1-N1** | (a) Unit: schema rejects — missing `revenue_ttm`, `revenue_ttm:"abc"`, `revenue_ttm:""`, `ebitda_sde:"n/a"`, `years_in_business:"three"`, missing `industry`, malformed `lead_email` — each with a field-keyed message. (b) Integration: `count(valuation)` and `count(activity_event where event_type='valuation_created')` **before === after** a batch of invalid POSTs; response is 422 with `errors` keyed by field. (c) E2E: submitting a non-numeric revenue shows an inline field error, `aria-invalid="true"`, focus moves to the first bad field, and **no** row is created. | unit + integration + E2E | mixed |
| **AC-S1-N2** | E2E: open `/valuation`, fill step 1 and part of step 2, then navigate away / close the page. Assert **(a)** no request to `/api/valuation` was issued (Playwright request listener / devtools network log), **(b)** `count(valuation)` and `count(activity_event)` unchanged. Code review gate: no autosave, no `beforeunload` beacon, no draft endpoint. | E2E | running app + local Supabase |
| **AC-X-07 / AC-X-16 / HARD-016** | `npm run check:tokens` exits 0 — no raw hex, no `rgb()/hsl()`, no `#fff`/`#000`, no arbitrary Tailwind values anywhere in `app/`, `components/`, `lib/`. | `scripts/check-tokens.mjs` | none |
| **HARD-015 / DEC-041** | axe-core scan of `/valuation` (form state, error state, result state): zero `color-contrast` violations; assert computed `font-size` ≥ 13px on all rendered text; assert the disclosure lines resolve to `var(--text)`, not `--text-muted`. | E2E (`@axe-core/playwright`) | running app |
| **INV-012** | Integration: after one successful POST, `select count(*) from activity_event where subject_entity_id = <id>` === 1. Then attempt `update`/`delete` on that row with the service key and assert it raises (`ae_append_only`). | Integration | local Supabase |
| **INV-010** | Integration: created row has `business_id IS NULL` and `converted_to_listing = false`. | Integration | local Supabase |
| **DEC-014** | Integration: POST a payload containing an `outputs` key with absurd values → either 422 (strict schema) or, if tolerated, the persisted `outputs` equals the recomputed engine result, never the client's. | Integration | local Supabase |
| **HARD-003** | Unit + E2E: SF scenario copy and the email contain the neutrality line and never the strings "we finance", "our loan", "Arxus lends"/"Arxus financing". | unit + E2E | mixed |

**Commands the builder must be able to run green:**
```
supabase start && supabase db reset      # applies 0001_init.sql unmodified
npm run check                            # check:tokens + unit tests
npm run test:int                         # integration (service role, local db)
npm run test:e2e                         # Playwright against `npm run dev`
```

---

## 6. External effects

**Exactly one: outbound email.** Isolated behind an interface so the terminal is testable with no provider.

```ts
// lib/email/mailer.ts
export interface EmailMessage { to: string; subject: string; html: string; text: string;
                                tags?: Record<string,string> }
export interface SendResult   { status: 'sent'|'failed'; message_id: string; provider: string;
                                error?: string }
export interface Mailer { send(msg: EmailMessage): Promise<SendResult> }
```

- **`LogMailer` (the only P0 implementation).** Emits a single structured JSON line to stdout
  (`{evt:'email.dispatch', to, subject, message_id, valuation_id}`) and, when `EMAIL_OUTBOX_DIR` is set,
  writes `${EMAIL_OUTBOX_DIR}/<message_id>.json` containing the full message. Never throws; on an internal
  error it returns `{status:'failed', …}`.
- **`getMailer()`** switches on `EMAIL_PROVIDER`. `'log'` is the only accepted value at P0; any other value
  throws at startup so a half-wired provider can never silently no-op.
- **No provider SDK, API key, domain, or webhook is added.** Wiring a real provider is a later change that
  touches `getMailer()` and nothing else.
- The email template is provider-agnostic: inline styles only, values interpolated from the same
  `lib/valuation/format.ts` the UI uses, so on-screen and in-email figures cannot drift.

**No other external effects.** No third-party analytics, no CRM, no webhooks, no funds/PII paths
(HARD-001/002 are not touched by this journey — no money, no SSN/bank fields).

**Environment (`.env.example`):**
```
NEXT_PUBLIC_SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=        # server-only; never NEXT_PUBLIC_
NEXT_PUBLIC_SITE_URL=http://localhost:3000
EMAIL_PROVIDER=log
EMAIL_FROM="Arxus <valuations@arxus.local>"
EMAIL_OUTBOX_DIR=.mail-outbox
```
(The anon key is deliberately absent — no browser Supabase client exists in this build.)

---

## 7. UI notes (token-bound)

- `/valuation` is a public marketing-adjacent surface → light `putty` core: page `--background`, form card
  `--surface`, hairline `--border`, primary action `--accent` with `--accent-ink`, focus ring `--focus`.
  `brandDeep` is available for a hero band (brand "Substance" layer) but is not required.
- Density: `guided` (the accessible end — DEC-040/041) for this consumer surface. `cardPadding: 28`,
  base font 16.5px.
- Motion: `motion.policy.data = easeOut` for the result reveal; **`instant`** for the disclosure/neutrality
  lines and for error appearance (no spring on money or disclosures).
- Numbers (range, comps, payments) use `.tnum` / `--font-mono` (JetBrains Mono, tabular-nums).
- Two client-side steps, one submit: Step 1 = the six business inputs; Step 2 = email + submit. The step
  transition is local state only — **no network call, no storage write** (AC-S1-N2).
- Errors: per-field message under the control, `aria-invalid`, `aria-describedby`, a summary region with
  `role="alert"`, and focus sent to the first invalid control. Server 422 errors render through the same
  component as client-side ones so the two paths cannot diverge.
- Result: value-range headline, `SourceTag` chips, comps table, three SF scenario cards, the two disclosure
  lines (estimate; no-platform-financing) at body size in `--text`, and the next-step CTA.

---

## 8. Open questions

These are **not** decided by the supplied canon. Each has a stated default so the builder is never blocked;
each default satisfies every AC-S1 criterion. If canon later rules otherwise, the change is one constant or
one file.

- **OQ-1 — Is the range shown before the email is captured?** JRN-S1's path reads "compute → capture email →
  send", which fixes the *processing* order but not whether the visitor may see the range without giving an
  email. **Default (implemented):** one submit; `lead_email` is required; the range is shown immediately after
  the successful POST. This is the only shape that keeps AC-S1-N2 clean (a compute-preview-then-gate design
  either persists before the email exists, or computes twice). Changing this is a form-flow change only — the
  endpoint contract is unaffected.
- **OQ-2 — Where does the next-step CTA point?** AC-S1-04 requires a next-step CTA; canon names the next step
  as JRN-S2 (list your business), which is **not built in this run**. **Default:** the *email* CTA points to
  `${NEXT_PUBLIC_SITE_URL}/valuation/<id>` (a page this run builds, so the link is never dead); the *result
  surface* CTA uses `NEXT_STEP_CTA` from `lib/constants.ts` (`{ label: 'List your business on Arxus',
  href: '/sell' }`). If `/sell` does not exist when JRN-S2 lands, the builder must not invent a listing
  surface — point `href` at the permalink too and leave the single constant as the swap point.
- **OQ-3 — The `owner_involvement` option set.** AC-S1-01 names the field; no canon document enumerates its
  values (`business` has no such column, so nothing is bound). **Default:** `absentee | part_time | full_time |
  owner_dependent`, stored in the untyped `valuation.inputs` jsonb — no schema conflict either way.
- **OQ-4 — The benchmark values themselves.** DEC-019 says P0 launches on "borrowed industry benchmarks", but
  canon supplies no multiples, comp set, or SF structures. The §4.1/§4.6 numbers are **seed placeholders**
  and must be labelled as such in `benchmarks.ts` (a file-header comment) — they are behind the
  `BenchmarkProvider` seam precisely so a sourced table replaces them without touching `compute.ts`. Someone
  with authority should ratify a real cold-start table; the acceptance criteria (low ≤ high, ≥1 comp, ≥1 SF,
  source-tagged) hold regardless of the values.
- **OQ-5 — Consent/marketing-permission for `lead_email`.** No AC-S1 criterion requires a Consent record, and
  the `consent` table requires `party_user_id NOT NULL → app_user`, which an anonymous visitor does not have —
  so a Consent row is structurally impossible here. **Default:** write no Consent row; render a plain-language,
  non-muted notice at the email field stating the address is used to send the result and follow-ups.
  A CAN-SPAM/marketing-consent posture for the free tool is a canon decision, not a build decision.
- **OQ-6 — Endpoint shape.** A Route Handler (`POST /api/valuation`) was chosen over a Server Action so the
  terminal and the negative cases are assertable with `fetch`/`curl` outside a browser. Not canon-governed;
  noted so it is not re-litigated mid-build.
- **OQ-7 — The valuation engine is deterministic arithmetic, not a model.** Kept that way deliberately: it
  keeps AC-S1-02/03 unit-testable and keeps AC-X-10/AC-X-14 (HumanReview, AI provenance labels) out of scope.
  **If anyone swaps in a model, this journey acquires `ai_action` + provenance-label obligations** — that is a
  canon-level change, not a refactor.
