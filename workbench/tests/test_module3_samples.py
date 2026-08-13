"""Independent-implementation conformance (first contact with the real Gateway).

Sadia's real Module 3 Gateway produced these samples. Everything until now was
validated only against our own mock, written to our own reading of the schemas.
This asserts the real-Gateway ExecutionEvent samples validate against the FROZEN
contract schemas, so a future contract or validator change is checked against an
independent implementation — not just our mock.

The four events came from THREE different runs, so only individual event shapes are
asserted here, never cross-event sequence contiguity. The `event-check-failed.json`
sample is a `check` event (message "CHK-TESTS FAILED (exit 1)"), NOT a terminal
`failed` event — it is treated purely as a check-event shape, nothing more.
"""

import json
from pathlib import Path

import pytest

from workbench import contract

SAMPLES = Path(__file__).parent / "fixtures" / "module3-samples"
EVENT_SAMPLES = [
    "event-accepted.json",
    "event-progress.json",
    "event-completed.json",
    "event-check-failed.json",
]


@pytest.mark.parametrize("name", EVENT_SAMPLES)
def test_real_gateway_event_validates_against_frozen_schema(name):
    ev = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
    res = contract.validate_event(ev)
    assert res["passed"], f"{name} failed contract validation: {res['errors']}"


def test_event_sample_set_is_stable():
    """Catch an accidental rename/removal of a real-Gateway sample."""
    assert {p.name for p in SAMPLES.glob("event-*.json")} == set(EVENT_SAMPLES)


def test_rejection_body_sample_is_well_formed():
    """The rejection body is a transport error envelope Module 1 stores verbatim, not a
    Module 2 contract message — there is no schema for it. Just keep the fixture valid."""
    body = json.loads((SAMPLES / "rejection-body.json").read_text(encoding="utf-8"))
    assert body["http_status"] == 400 and isinstance(body["description"], str)
