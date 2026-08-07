# Billing ledger

The billing ledger turns a subscription's **active entitlements** into invoice
lines, once per billing cycle.

## Cycle run

When a subscription reaches `renews_at`, the ledger opens a new invoice for that
cycle and generates its invoice lines. The invoice is then handed to the payment
gateway (out of scope for this package).

## Invoice line

Each billable entitlement that is active for the subscription in that cycle
produces exactly one invoice line, carrying a `ratecard_code`, a `description`
and an `amount_minor` copied from the ratecard entry, and a `quantity`.

## Ratecard

The **ratecard** is the ledger's price list, versioned alongside the plan
catalogue. Each entry has a `ratecard_code`, a `description`, an `amount_minor`,
and a `billable` flag. Only billable entries produce a line; functional-only
entitlements never appear on an invoice even when active.

## Reprocessing

Because plans, entitlements, and the ratecard are all versioned, a past cycle can
be re-run and reproduces the same invoice lines. Always price a line from the
ratecard version in force at `renews_at`, not today's.
