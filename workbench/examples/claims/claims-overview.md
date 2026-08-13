---
name: Claims Adjudication
version: "2.1"
metadata:
  owner: Claims platform group
  domain: property-claims
  reviewed: "2026-08-05"
---
# Claims Adjudication — knowledge package

Meridian Claims is our property-insurance claims platform. It receives a claim,
validates the claimant and policy, gathers supporting evidence, and adjudicates
the claim into an approved payout, a partial settlement, or a denial with a
documented reason.

This package is the reference an engineer (or an agent) uses to change the
adjudication behaviour correctly. Unlike some packages, it declares no
front-matter dependencies — every dependent document is reached through ordinary
relative Markdown links below, followed recursively.

## Documents

- [Claim data model](domain/claim-model.md) — the claim, policy, and evidence
  records and the states a claim moves through.
- [Adjudication pipeline](architecture/adjudication-pipeline.md) — the stages a
  claim passes through from intake to a final decision.
- [Adjudication rules](procedures/adjudication-rules.md) — how coverage,
  deductibles, and limits combine into an approved amount or a denial.

## Scope

- **In scope:** claim intake, policy/coverage validation, evidence checks,
  deductible and limit application, and the final adjudication decision.
- **Out of scope:** premium billing, fraud investigation tooling, reinsurance,
  and the claimant-facing mobile app.

## Conventions

- All monetary amounts are stored in minor units (cents) as integers.
- Every coverage and limit carries an `effective_date` so a claim is always
  adjudicated against the policy terms in force on its loss date.
- Decisions are immutable once issued; a correction is a new decision that
  supersedes the prior one.
