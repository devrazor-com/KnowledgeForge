# Account and subscription data model

This document describes the records that hold a customer's subscription state.

## `account` record

One per customer: `account_id`, `display_name`, `created_at`, and a `status` of
`active`, `suspended`, or `closed`.

## `subscription` record

One per active plan a customer holds. An account may have more than one.

| Field | Type | Notes |
|---|---|---|
| `subscription_id` | string | Stable identifier. |
| `account_id` | string | Owning account. |
| `plan_code` | string | The plan this subscription instantiates. |
| `plan_version` | integer | The plan version in force for this subscription. |
| `renews_at` | timestamp | Start of the next billing cycle. |
| `entitlement_flags` | object | See below. |

### `entitlement_flags`

`entitlement_flags` is a map from entitlement code to a boolean:

```json
"entitlement_flags": { "seats_10": true, "audit_log": true, "sso": false }
```

- `true` means the entitlement is active on this subscription, regardless of the
  plan default.
- `false` explicitly turns off an entitlement the plan would otherwise grant.
- An absent code falls back to the plan's default grant.

The **effective** entitlement set is the plan's default grants overlaid with the
explicit flags here.

> Note: this document defines where the flag is stored and what it means. What a
> flag causes downstream — in particular whether and how it appears on an
> invoice — is the billing ledger's concern, not this record's.
