#!/usr/bin/env python3
"""Validate every POC fixture against the frozen Module 2 schemas.

Standalone tooling for Step 1 — not imported by Mod1 or Mod3. It loads the four
schemas from ../contract/ (sibling of poc/), builds a referencing registry so
the relative $refs resolve with no URL mapping, and validates:

  * the two task fixtures against validation-request.schema.json#/$defs/ValidationTask
  * the three canned results against validation-result.schema.json
  * every event in the three canned sequences against execution-event.schema.json

It also runs contract sanity checks on each event sequence (contiguous
sequence numbers from 1, exactly one terminal event, and it comes last).

A fixture that fails here is a bug in the fixture, not in the schema.
"""

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

POC_DIR = Path(__file__).resolve().parent
CONTRACT_DIR = POC_DIR.parent / "contract"
FIX = POC_DIR / "fixtures"

SCHEMA_FILES = [
    "validation-request.schema.json",
    "execution-event.schema.json",
    "validation-result.schema.json",
    "failure-diagnosis.schema.json",
]

TERMINAL = {"completed", "failed", "cancelled"}


def load_registry() -> Registry:
    resources = []
    for name in SCHEMA_FILES:
        contents = json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))
        # Register under the bare-filename $id so relative $refs resolve.
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


def validator_for(schema: dict, registry: Registry) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=registry)


def report(label: str, errors: list[str]) -> bool:
    if errors:
        print(f"  FAIL  {label}")
        for e in errors:
            print(f"          - {e}")
        return False
    print(f"  ok    {label}")
    return True


def schema_errors(validator: Draft202012Validator, instance) -> list[str]:
    out = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        out.append(f"{loc}: {err.message}")
    return out


def sequence_errors(events: list) -> list[str]:
    errs = []
    if not events:
        return ["event sequence is empty"]
    run_ids = {e.get("run_id") for e in events}
    if len(run_ids) != 1:
        errs.append(f"mixed run_ids in one sequence: {sorted(run_ids)}")
    seqs = [e.get("sequence") for e in events]
    expected = list(range(1, len(events) + 1))
    if seqs != expected:
        errs.append(f"sequence numbers {seqs} are not the contiguous run {expected}")
    terminals = [i for i, e in enumerate(events) if e.get("event_type") in TERMINAL]
    if len(terminals) != 1:
        errs.append(f"expected exactly one terminal event, found {len(terminals)}")
    elif terminals[0] != len(events) - 1:
        errs.append("terminal event is not the last event in the sequence")
    return errs


def main() -> int:
    registry = load_registry()
    task_v = validator_for(
        {"$ref": "validation-request.schema.json#/$defs/ValidationTask"}, registry
    )
    result_v = validator_for(
        {"$ref": "validation-result.schema.json"}, registry
    )
    event_v = validator_for(
        {"$ref": "execution-event.schema.json"}, registry
    )

    ok = True

    print("ValidationTask fixtures  (validation-request.schema.json#/$defs/ValidationTask)")
    for p in sorted((FIX / "tasks").glob("*.json")):
        inst = json.loads(p.read_text(encoding="utf-8"))
        ok &= report(p.name, schema_errors(task_v, inst))

    print("\nValidationResult fixtures  (validation-result.schema.json)")
    for p in sorted((FIX / "results").glob("result-*.json")):
        inst = json.loads(p.read_text(encoding="utf-8"))
        ok &= report(p.name, schema_errors(result_v, inst))

    print("\nExecutionEvent sequences  (execution-event.schema.json + contract sanity)")
    for p in sorted((FIX / "results").glob("events-*.json")):
        name = p.name
        events = json.loads(p.read_text(encoding="utf-8"))
        errs = []
        for i, ev in enumerate(events):
            errs += [f"event[{i}] {m}" for m in schema_errors(event_v, ev)]
        errs += sequence_errors(events)
        ok &= report(f"{name}  ({len(events)} events)", errs)

    print("\n" + ("ALL FIXTURES VALID" if ok else "SOME FIXTURES FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
