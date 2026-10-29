"""An evaluator that states an unmet requirement names it on the failed stage's decoding.

The `error` record's kind is the evaluator's own statement that the measured revision lacks a
capability the task requires. The decoding keeps the requirement whatever the exit status, so
the framework can tell the failure is deterministic (core takes no further run of the
revision). A record without a kind names none and is classified as before: the protocol's
backward-compatible default.
"""

from __future__ import annotations

import json

from hypothesis import example, given
from hypothesis import strategies as st

from vs_runtime.api import BenchmarkFailureKind
from vs_runtime.api.infrastructure import (
    ProtocolBenchmarkContract,
    TrustedBenchmarkDecoding,
    classify,
    decode_trusted_benchmark_run,
    signal_of,
)

_HELLO: dict[str, object] = {
    "kind": "hello",
    "protocol": 2,
    "metrics": {"tok_s": {"direction": "max"}},
}
_REQUIREMENTS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=20)


def _framed(*records: dict[str, object]) -> str:
    body = "".join(json.dumps(record) + "\n" for record in records)
    return (
        f"log line\n__VIBESYS_FRAMEWORK_BENCHMARK_JSON__\n{body}"
        "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__\n"
    )


def _decode(framed: str, *, exited_cleanly: bool) -> TrustedBenchmarkDecoding:
    return decode_trusted_benchmark_run(
        framed, ProtocolBenchmarkContract(), frozenset(), exited_cleanly=exited_cleanly
    )


@given(requirement=_REQUIREMENTS, declared=st.booleans(), clean=st.booleans())
@example(requirement="prefix_cache", declared=False, clean=False)
def test_an_unmet_requirement_is_kept_whatever_the_exit_status(
    requirement: str, *, declared: bool, clean: bool
) -> None:
    error: dict[str, object] = {
        "kind": "error",
        "message": "no cache hits",
        "error_kind": "requirement_not_met",
        "requirement": requirement,
    }

    decoded = _decode(_framed(*([_HELLO] if declared else []), error), exited_cleanly=clean)

    assert not decoded.passed
    assert decoded.requirement == requirement
    assert decoded.reason == "no cache hits"


@given(exit_code=st.integers(min_value=0, max_value=255), declared=st.booleans())
def test_an_error_without_a_kind_names_no_requirement_and_is_classified_as_before(
    exit_code: int, *, declared: bool
) -> None:
    framed = _framed(*([_HELLO] if declared else []), {"kind": "error", "message": "failed"})

    decoded = _decode(framed, exited_cleanly=False)

    assert decoded.requirement is None
    assert classify(signal_of(exit_code), decoded.record) is BenchmarkFailureKind.WORKLOAD


@given(clean=st.booleans())
def test_a_run_that_measured_a_row_names_no_requirement(*, clean: bool) -> None:
    result: dict[str, object] = {"kind": "result", "values": {"tok_s": 2.5}}

    decoded = _decode(_framed(_HELLO, result), exited_cleanly=clean)

    assert decoded.requirement is None
