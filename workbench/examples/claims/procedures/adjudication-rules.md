# Adjudication rules

These rules turn an assessed loss into an approved amount. They assume the claim
already passed validation with a coverage matching its `peril` (see the
[claim data model](../domain/claim-model.md) for the records involved).

## Approved amount

Given the assessed loss `L` (cents), the matching coverage's `deductible_cents`
`D` and `limit_cents` `M`:

```
approved = clamp(L - D, 0, M)
```

- If `L <= D`, the deductible absorbs the whole loss → `approved = 0` → **denied**
  with reason `below_deductible`.
- If `0 < L - D < M` → **approved** for `L - D`.
- If `L - D >= M` → **partial**, paid at the coverage limit `M`, reason
  `limit_reached`.

## Decision reasons

Every decision records a machine-readable `reason`:

- `approved_in_full`
- `limit_reached`
- `below_deductible`
- `no_coverage`
- `insufficient_evidence`

## Ordering

Deductible is applied **before** the limit. Applying them in the reverse order
overpays claims that exceed the limit, so the order is fixed and load-bearing.
