# Billing ledger

The billing ledger turns a subscription's **active entitlements** into invoice
lines, once per billing cycle.

## Cycle run

When a subscription reaches `renews_at`, the ledger opens a new **invoice** for
that cycle and generates its **invoice lines**. The invoice is then handed to
the payment gateway (out of scope for this package).

## Invoice line

Each billable entitlement that is active for the subscription in that cycle
produces exactly one invoice line:

| Field | Type | Notes |
|---|---|---|
| `line_id` | string | |
| `invoice_id` | string | Owning invoice. |
| `ratecard_code` | string | The ratecard entry this line is priced from. |
| `description` | string | Human-readable, copied from the ratecard entry. |
| `amount_minor` | integer | Price in cents, copied from the ratecard entry. |
| `quantity` | integer | Usually 1. |

## Ratecard

The **ratecard** is the ledger's price list. It is versioned alongside the plan
catalogue. Each ratecard entry is:

| Field | Type | Notes |
|---|---|---|
| `ratecard_code` | string | Identifier for this priced item. |
| `description` | string | Appears on the invoice line. |
| `amount_minor` | integer | Price in cents. |
| `billable` | boolean | If false, the item is tracked but never invoiced. |

Only entitlements that are **billable** produce a line; functional-only
entitlements never appear on an invoice even when active.

## Reprocessing

Because plans, entitlements, and the ratecard are all versioned, a past cycle
can be re-run and will reproduce the same invoice lines. Always price a line
from the ratecard version that was in force at `renews_at`, not today's.
