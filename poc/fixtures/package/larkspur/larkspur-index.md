# Larkspur Subscriptions — knowledge package

Larkspur is our recurring-subscription platform. It manages customer
subscriptions, the entitlements each subscription grants, and the billing
ledger that turns those entitlements into invoice lines every cycle.

This package is the reference an engineer (or an agent) uses to make changes
to Larkspur correctly. Start here, then read the linked documents.

## Documents

- [Plan and entitlement rules](rules/plan-rules.md) — the plans we sell, the
  tiers, and how entitlements are named and versioned.
- [Account and subscription data model](data/account-model.md) — the customer
  account and subscription records, including where entitlement flags live.
- [Billing ledger](data/billing-ledger.md) — how each billing cycle produces
  invoice lines from a subscription's active entitlements.

## Scope

- **In scope:** subscription lifecycle, entitlement flags, plan definitions,
  and the billing ledger that invoices against them.
- **Out of scope:** the payment gateway, dunning, tax calculation, and the
  customer-facing web app. Those live in other packages.

## Conventions

- All record fields use `snake_case`.
- Money is stored in minor units (cents) as integers.
- Every plan and entitlement carries a `version` so historical invoices remain
  reproducible.
