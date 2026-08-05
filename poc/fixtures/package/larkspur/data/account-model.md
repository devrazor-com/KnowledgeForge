# Account and subscription data model

This document describes the records that hold a customer's subscription state.

## `account` record

One per customer.

| Field | Type | Notes |
|---|---|---|
| `account_id` | string | Stable customer identifier. |
| `display_name` | string | Company or person name. |
| `created_at` | timestamp | |
| `status` | string | `active`, `suspended`, or `closed`. |

## `subscription` record

One per active plan a customer holds. An account may have more than one.

| Field | Type | Notes |
|---|---|---|
| `subscription_id` | string | Stable identifier. |
| `account_id` | string | Owning account. |
| `plan_code` | string | The plan this subscription instantiates. |
| `plan_version` | integer | The plan version in force for this subscription. |
| `started_at` | timestamp | |
| `renews_at` | timestamp | Start of the next billing cycle. |
| `entitlement_flags` | object | See below. |

### `entitlement_flags`

`entitlement_flags` is where per-subscription entitlement state lives. It is a
map from entitlement code to a boolean:

```json
"entitlement_flags": {
  "seats_10": true,
  "audit_log": true,
  "sso": false
}
```

- A flag set to `true` means the entitlement is **active** on this subscription,
  regardless of whether the plan grants it by default.
- A flag set to `false` explicitly turns off an entitlement the plan would
  otherwise grant.
- An entitlement code absent from the map falls back to the plan's default
  grant.

The **effective** entitlement set for a subscription is therefore the plan's
default grants, overlaid with the explicit `true`/`false` flags here.

### Adding a new entitlement flag

To make a new entitlement available on subscriptions, add its code to
`entitlement_flags` where it applies and set the boolean. The entitlement code
must already exist in the [plan and entitlement rules](../rules/plan-rules.md).

> Note: this document defines *where the flag is stored and what it means*. What
> a flag causes to happen downstream — in particular whether and how it appears
> on an invoice — is the billing ledger's concern, not this record's.
