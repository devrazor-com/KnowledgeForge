# Claim data model

A **claim** is filed against a **policy** for a **loss** that occurred on a
specific date. Adjudication decides how much, if anything, the policy pays.

## Records

- `claim`
  - `id`
  - `policy_id`
  - `loss_date` — the date the damage occurred; coverage is evaluated as of this date
  - `reported_at`
  - `peril` — e.g. `water`, `fire`, `wind`, `theft`
  - `claimed_amount_cents`
  - `state` — see the lifecycle below
- `policy`
  - `id`
  - `holder_id`
  - `coverages[]` — each with `peril`, `limit_cents`, `deductible_cents`, `effective_date`
- `evidence[]`
  - `claim_id`
  - `kind` — e.g. `photo`, `police_report`, `repair_estimate`
  - `received_at`

## Claim lifecycle

`intake → validating → gathering_evidence → adjudicating → decided`

A `decided` claim carries a `decision` of `approved`, `partial`, or `denied`,
with an `amount_cents` (0 for a denial) and a human-readable `reason`.

## Notes

- A claim with no coverage for its `peril` on the `loss_date` is denied at
  validation, before evidence gathering.
- The claimed amount never caps the payout upward; the coverage `limit_cents`
  and `deductible_cents` do, per the adjudication rules.
