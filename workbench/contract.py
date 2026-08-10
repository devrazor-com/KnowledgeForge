"""Module 2 contract adapter — Module 1's OWN schema validation.

Loads the four frozen schemas from contract/ and validates messages at the
boundary: the outbound ValidationRequest before it is sent, and every inbound
ExecutionEvent and ValidationResult on receipt. Module 3 (or the mock) validates
independently on its side; nothing here trusts or fabricates that.

This is the only place Module 1 reads the contract. contract/ is frozen and is
never modified.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from workbench.config import REPO_ROOT

CONTRACT_DIR = REPO_ROOT / "contract"

_SCHEMA_FILES = [
    "validation-request.schema.json",
    "execution-event.schema.json",
    "validation-result.schema.json",
    "failure-diagnosis.schema.json",
]


def _build_registry() -> Registry:
    resources = []
    for name in _SCHEMA_FILES:
        contents = json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


_REGISTRY = _build_registry()
_REQUEST = Draft202012Validator({"$ref": "validation-request.schema.json"}, registry=_REGISTRY)
_EVENT = Draft202012Validator({"$ref": "execution-event.schema.json"}, registry=_REGISTRY)
_RESULT = Draft202012Validator({"$ref": "validation-result.schema.json"}, registry=_REGISTRY)


def _report(validator: Draft202012Validator, instance: Any) -> dict:
    """A plain {passed, errors} report suitable for the UI and for storage."""
    errors = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{loc}: {err.message}")
    return {"passed": not errors, "errors": errors}


def validate_request(instance: Any) -> dict:
    return _report(_REQUEST, instance)


def validate_event(instance: Any) -> dict:
    return _report(_EVENT, instance)


def validate_result(instance: Any) -> dict:
    return _report(_RESULT, instance)
