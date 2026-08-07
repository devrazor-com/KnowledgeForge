# Plan and entitlement rules

A **plan** is something a customer can subscribe to. A **subscription** is one
customer's active instance of a plan. A plan grants a set of **entitlements** —
named capabilities the subscription is allowed to use.

## Plans

Each plan has a `plan_code`, a `version`, a `tier` (`starter`, `team`, or
`business`), and a list of `grants` — the entitlement codes it turns on by
default. Higher tiers are supersets of lower ones.

## Entitlements

An **entitlement** is a named capability. Entitlement codes are global and
`snake_case`, and never reused for a different meaning. An entitlement is either
on or off for a subscription; there is no partial state.

A plan's `grants` set the default entitlements. Individual subscriptions may turn
additional entitlements on or off — a negotiated add-on, or a temporary promotion.

## Billable vs. non-billable entitlements

Some entitlements are **billable** (they add to the invoice); others are purely
functional. Whether an entitlement is billable, and at what price, is a property
of the billing ledger's ratecard, not of this document. This document only
defines what entitlements exist and which plans grant them by default.
