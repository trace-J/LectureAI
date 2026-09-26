# Handoff: P6, Stripe and entitlements (slice 4)

Paste the "Session 1" block below into a new session in `~/apps/syllabus-accounts`.
The rest of this file is the phase plan that session should read.

---

## Before any session: what only Trace can do

These are Stripe dashboard actions. No session can do them and every one of
them blocks something below.

1. Create the Stripe account under **Main Course Media LLC** (Texas).
2. **Turn on Stripe Tax before the first dollar.** Texas taxes SaaS as a data
   processing service, 20% exempt, so about 80% of each sale is taxable. Set
   the product tax code to SaaS and register Texas as a tax jurisdiction.
3. Create three recurring monthly Products/Prices and note the price IDs:
   - Starter, $9
   - Standard, $15
   - Pro, $25
4. Create the friends-and-family promotion code: `percent_off: 100`,
   `duration: forever`.
5. Put the keys in as Worker secrets (test keys first):
   `npx wrangler secret put STRIPE_SECRET_KEY`
   `npx wrangler secret put STRIPE_WEBHOOK_SECRET`
   Same names in `.dev.vars` for local dev. `.dev.vars` is gitignored.

Sessions 1 and 2 can be built and tested against the Stripe CLI and fixtures
without any of this. Session 3 onward needs at least the test-mode keys.

---

## The shape of the phase

Four PRs, one session each, per SESSIONS.md. Branch prefix `trace-J/stripe-`.

| # | PR | What lands | Needs |
|---|---|---|---|
| 4a | `trace-J/stripe-schema` | `subscriptions` table, tier table in code, allowance derivation, entitlement read path in the proxy | nothing |
| 4b | `trace-J/stripe-webhook` | `/stripe/webhook`: signature verification, idempotency on event id, writes `allowances` rows | 4a |
| 4c | `trace-J/stripe-checkout` | Checkout session, Billing Portal, the billing section of the account page | 4b, test keys |
| 4d | `trace-J/stripe-trial` | 5-hour trial entitlement, ending the trial early when hours are spent, the top-up path | 4c |

---

## What already exists, and is the whole point

Read these before writing anything. The schema was built for this slice.

- `migrations/0005_usage.sql`: the **`allowances`** table already exists:
  `account_id`, `audio_seconds`, `summary_tokens`, `source`, `updated_at`.
  Its comment says "Slice 4 (Stripe and entitlements) is what starts writing
  rows here; nothing else should." That is this work.
- `src/db.ts`: `allowance()` and `putAllowance()` are already written.
- `src/proxy.ts:196`: `allowanceFor()` reads that row and falls back to
  `TRIAL_ALLOWANCE` (5 hours of audio, 150k summary tokens) when there is
  none. **Every account is on that fallback today.**
- `src/proxy.ts`: `GLOBAL_CEILING`, per-account rate limits, and the
  reserve/settle usage flow (`migrations/0007`) are done. Do not rebuild any
  of it.

So the entitlement enforcement point already exists and already works. Stripe
is not adding enforcement; it is adding the thing that writes the row the
enforcement already reads. Resist the urge to touch the proxy's spending path.

## Hard rules for this phase

- **The proxy is the only enforcement point.** The panel UI may hide, dim, and
  explain. It runs on the user's Mac and must never be trusted to enforce.
- **A cap is a hard stop with a one-click top-up.** No surprise bills for
  students, ever. That is a locked decision.
- Never write an `allowances` row from anywhere but the Stripe webhook.
- `source` on the allowance row records where it came from: `trial`,
  `starter`, `standard`, `pro`, `comp`, `topup`. It is how a support question
  gets answered a month later.

## Gotchas that will cost you an hour each

- **The webhook must bypass the auth middleware.** `src/index.ts` runs an
  auth middleware on `*` that resolves a device bearer or a session cookie.
  Stripe carries neither. Mount the webhook so it authenticates on the Stripe
  signature alone.
- **Signature verification needs the RAW body**, before any JSON parsing.
  Use `await c.req.text()`, not `c.req.json()`.
- **Workers cannot use the Stripe SDK's default HTTP client or crypto.**
  Construct with `Stripe.createFetchHttpClient()`, and verify with
  `stripe.webhooks.constructEventAsync(...)` plus
  `Stripe.createSubtleCryptoProvider()`. The synchronous `constructEvent`
  throws in workerd.
- **Idempotency is on the Stripe event id**, stored in a table with a unique
  constraint, checked before the handler runs. Stripe retries, and retries
  are how an account silently gets two months of allowance.
- **Add the new secrets to `vitest.config.ts`.** Test bindings are declared
  there explicitly; a missing one fails as an undefined at runtime, not as a
  clear config error.
- **The test suite has a known flake.** `vitest-pool-workers` deadlocks at a
  varying rate, at startup or after every test has passed. Check the
  per-file counts before blaming your branch. CI retries it 6 times.

## Two decisions this phase has to make, and should not invent quietly

1. **Summary tokens per tier.** The trial is 5 hours to 150k tokens, which is
   30k tokens per hour, deliberately loose for retries. A real lecture hour
   models at ~9.5k. Applying the trial's ratio gives Starter 450k / Standard
   900k / Pro 1.35M. Applying the modeled ratio with slack gives roughly a
   third of that. Pick one, write down which and why, because it decides
   whether the token meter or the hour meter is the one users actually hit.
   HOME-STRETCH prices the tiers on the HOUR cap, so the token cap should be
   headroom and never the binding limit.
2. **Pro's 15 assistant sessions cannot be metered yet.** There is no
   `/proxy/assistant` endpoint; the study assistant runs on one Mac's own
   Anthropic key and bypasses the account service entirely. So Pro's
   allowance row can carry audio and tokens but has nothing to spend
   assistant sessions against. Either add the third column now and leave it
   unenforced, or leave it out and take the migration later. Decide in 4a;
   do not discover it in 4d.

## The tiers, from HOME-STRETCH.md

| Tier | Price | Audio | Notes |
|---|---|---|---|
| Starter | $9 | 15 hr | ~2 courses |
| Standard | $15 | 30 hr | ~4 courses |
| Pro | $25 | 45 hr | all of them, plus 15 assistant sessions |

No free tier. The 100%-off promotion code is how friends and family get in.
Trial is 5 hours with the card collected up front.

---

## Session 1 prompt (copy from here)

> Read HOME-STRETCH.md, SESSIONS.md and HANDOFF-STRIPE.md in ~/apps/LectureAI
> first; they are local-only planning docs and are not in this repo. We are
> doing **P6 slice 4a** in syllabus-accounts: the subscriptions schema, the
> tier table, and the read path that turns a subscription into the
> `allowances` row the proxy already enforces. Branch
> `trace-J/stripe-schema`. Use a worktree, and run `npm install` in it.
>
> Scope for this PR, and nothing beyond it:
>
> - A `subscriptions` table: account, Stripe customer id, Stripe subscription
>   id, price id, tier, status, current period end, cancel-at-period-end,
>   created/updated. Plus a `stripe_events` table for webhook idempotency,
>   unique on the event id.
> - One tier table in code mapping price id to tier to allowance, with the
>   audio hours above. Make the token-per-tier call the handoff flags and say
>   in a comment why.
> - The function that derives an allowance row from a subscription, and its
>   tests. No Stripe API calls and no webhook yet: this PR is the shape of
>   the data and the rules that read it.
> - Confirm, with a test, that an account with no subscription still gets
>   `TRIAL_ALLOWANCE` exactly as it does today. This slice must not change
>   behavior for any account until a webhook writes a row.
>
> Do not touch the proxy's reserve/settle path, the rate limits, or the
> global ceiling. Do not add Stripe SDK calls yet.
>
> Stage named paths only, never `git add -A`. Note that neither repo has
> branch protection, so CI reports but does not block; read it anyway before
> merging. Copy standards apply to anything user-facing: no em dashes, US
> spelling.
