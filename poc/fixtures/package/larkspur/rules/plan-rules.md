# Plan and entitlement rules

A **plan** is something a customer can subscribe to. A **subscription** is one
customer's active instance of a plan. A plan grants a set of **entitlements** —
named capabilities the subscription is allowed to use.

## Plans

Each plan has:

- `plan_code` — stable identifier, e.g. `team_monthly`.
- `version` — integer, bumped whenever the plan's grants change.
- `tier` — one of `starter`, `team`, `business`.
- `grants` — the list of entitlement codes this plan turns on by default.

Higher tiers are supersets: everything a `team` plan grants, a `business` plan
grants too, plus its own additions.

## Entitlements

An **entitlement** is a named capability. Entitlement codes are global (not
per-plan) so the same capability means the same thing everywhere.

Examples of entitlement codes in use today:

- `seats_10` — up to ten member seats.
- `audit_log` — access to the audit log export.
- `sso` — single sign-on.

Rules:

- Entitlement codes are `snake_case` and never reused for a different meaning.
- An entitlement is either **on** or **off** for a subscription. There is no
  partial state.
- A plan's `grants` set the *default* entitlements. Individual subscriptions may
  turn additional entitlements on or off (see the account data model) — for
  example a negotiated add-on, or a temporary promotion.

## Billable vs. non-billable entitlements

Some entitlements are **billable** (they add to the invoice), others are purely
functional. Whether an entitlement is billable, and at what price, is **not**
decided here — it is a property of the billing ledger's ratecard. This document
only defines what entitlements exist and which plans grant them by default.
