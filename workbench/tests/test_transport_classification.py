"""Transport-failure classification (Step 3B-1).

`gateway_client._classify` + `NON_DELIVERY_REASONS` are the SOLE gate for the
strongest positive claim in the cancellation-delivery model: `undelivered` means
Module 1 has transport evidence that this attempt did not reach the configured
Gateway endpoint. A future refactor must not silently widen what counts as
non-delivery, so this pins the boundary exactly.

Classification is structural (exception type + errno), never error-message-text
matching — fragile across OSes, runtime versions, localization, and Windows.
"""

import errno
import socket
import urllib.error

import pytest

from workbench.gateway_client import NON_DELIVERY_REASONS, _classify


def _wrapped(cause):
    """How urllib surfaces a transport error: URLError with the cause in .reason."""
    return urllib.error.URLError(cause)


# (label, exception, expected reason, expected: counts as non-delivery?)
CASES = [
    ("connection refused (ConnectionRefusedError)",
     _wrapped(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")), "refused", True),
    ("connection refused (bare OSError errno fallback)",
     _wrapped(OSError(errno.ECONNREFUSED, "refused")), "refused", True),
    ("DNS / name resolution (socket.gaierror)",
     _wrapped(socket.gaierror(socket.EAI_NONAME, "Name or service not known")), "dns", True),
    ("connection reset (may follow a partial send)",
     _wrapped(ConnectionResetError(errno.ECONNRESET, "reset")), "other", False),
    ("socket timeout (connect or read)",
     _wrapped(socket.timeout("timed out")), "timeout", False),
    ("TimeoutError (socket.timeout is TimeoutError)",
     _wrapped(TimeoutError("timed out")), "timeout", False),
    ("OS-level ETIMEDOUT (mapped to TimeoutError)",
     _wrapped(OSError(errno.ETIMEDOUT, "Operation timed out")), "timeout", False),
    ("broken pipe",
     _wrapped(BrokenPipeError(errno.EPIPE, "broken pipe")), "other", False),
    ("non-exception / string reason (defensive)",
     urllib.error.URLError("some string reason"), "other", False),
]


@pytest.mark.parametrize("label, exc, expected_reason, is_non_delivery",
                         CASES, ids=[c[0] for c in CASES])
def test_classify(label, exc, expected_reason, is_non_delivery):
    reason = _classify(exc)
    assert reason == expected_reason, f"{label}: reason {reason!r} != {expected_reason!r}"
    # The membership check is the real correctness boundary.
    assert (reason in NON_DELIVERY_REASONS) is is_non_delivery, (
        f"{label}: non-delivery membership wrong for reason {reason!r}")


def test_non_delivery_reasons_are_exactly_refused_and_dns():
    """Guard: only ECONNREFUSED and DNS resolution failure are positive evidence of
    non-delivery. If a refactor widens this set, that is a deliberate change to the
    strongest claim in the model and must fail here first."""
    assert NON_DELIVERY_REASONS == frozenset({"refused", "dns"})


def test_only_refused_and_dns_map_into_the_non_delivery_set():
    """No enumerated indeterminate failure may classify as non-delivery."""
    for label, exc, _reason, is_non_delivery in CASES:
        if not is_non_delivery:
            assert _classify(exc) not in NON_DELIVERY_REASONS, label
