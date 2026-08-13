# Adjudication pipeline

A claim moves through a fixed sequence of stages. Each stage either advances the
claim or moves it directly to a `decided` state with a denial reason.

1. **Intake** — normalise the submission, attach it to a `policy_id`, and record
   the `loss_date` and `peril`.
2. **Validating** — confirm the policy exists, is active on the `loss_date`, and
   has a coverage matching the claim's `peril`. No matching coverage → immediate
   denial (`no_coverage`).
3. **Gathering evidence** — collect the required evidence for the `peril`. A
   missing mandatory item holds the claim here; it is never silently skipped.
4. **Adjudicating** — apply the coverage limit and deductible to the assessed
   loss to produce an approved amount. See the adjudication rules.
5. **Decided** — emit an immutable `decision` (`approved` / `partial` / `denied`)
   with an `amount_cents` and a `reason`.

## Idempotency

Re-running the pipeline for a claim already in `decided` is a no-op: the existing
decision stands. A correction is issued as a new, superseding decision rather than
by mutating the pipeline's output.
