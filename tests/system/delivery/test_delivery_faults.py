"""C4: unknown, retried, duplicated, reordered and reloaded observations decide the same.

The skeleton search runs on the real core under each delivery fault (`vs_core.testing.drive.Faults`)
and under any combination of them, all at once included. The executors' answers are the same; only how and how often core
hears them changes. So the search must submit the same decisions (apart from each wait's absolute
limit, which moves by what a retry costs on the run clock), the run must end with the same
result, and it must be live.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.system.delivery._skeleton_subject import (
    Faulted,
    Outcomes,
    harness,
    launch,
)

from vs_core.testing.drive import Faults, Trace, drive
from vs_core.testing.liveness import Budget, End, Journal, assert_live
from vs_sim.api.testing import examples

_EXAMPLES = examples(pr=3, nightly=12)
PURE = settings(
    max_examples=_EXAMPLES,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

# Core re-asks an adoption verification that came back unknown, in a new adoption round, rather
# than waiting for a later answer to it; a verification that is unknown every time is an
# adoption that failed, typed. `test_request_kinds.py` holds the one-unknown case.
_NOT_VERIFICATION = {"unknown_first": lambda request: request.kind != "verify_adoption"}

DELIVERY_FAULTS = {
    "unknown_first": Faults(unknown_first=_NOT_VERIFICATION["unknown_first"]),
    "retry_first": Faults(retry_first=lambda _request: True),
    "duplicate": Faults(duplicate=True),
    "reorder": Faults(reorder=True),
    "reload": Faults(reload=True),
}

outcomes = st.builds(
    Outcomes,
    measurements=st.lists(
        st.sampled_from(("succeeded", "workload_failure", "infrastructure_failure")), max_size=3
    ).map(tuple),
    turns=st.lists(st.sampled_from(("replies", "unparsable")), max_size=3).map(tuple),
)


def _run(chosen: Outcomes, faults: Faults) -> Trace:
    spec = launch()
    hosting = harness()
    driver = spec.search.driver(spec.config, hosting.registry)
    trace = drive(driver, spec.config, Faulted(chosen), hosting, faults)
    end = End.TERMINAL if trace.finished else End.CUT_SHORT
    budget = Budget(retries=hosting.limits.request_retries.limit)
    assert_live(Journal.from_log(trace.log), trace.core, budget, end)
    return trace


def _decisions(trace: Trace) -> list[str]:
    """The decisions as JSON, minus each wait's absolute limit."""
    found = []
    for decision in trace.decisions:
        data = decision.model_dump(mode="json")
        data.pop("until", None)
        found.append(json.dumps(data, sort_keys=True))
    return found


# Every fault at once is `test_c4_any_combination_of_delivery_faults_...`: it pins that set.
@pytest.mark.parametrize("fault", sorted(DELIVERY_FAULTS))
@PURE
@given(chosen=outcomes)
def test_c4_delivery_faults_never_change_the_decisions_or_the_liveness(
    fault: str, chosen: Outcomes
) -> None:
    plain = _run(chosen, Faults())
    faulty = _run(chosen, DELIVERY_FAULTS[fault])
    assert _decisions(faulty) == _decisions(plain)
    assert faulty.finished == plain.finished
    assert faulty.core.run.result == plain.core.run.result


_FAULT_FLAGS = sorted(DELIVERY_FAULTS)


def _combined(chosen: frozenset[str]) -> Faults:
    """The faults named in ``chosen`` at once."""
    found = Faults(
        duplicate="duplicate" in chosen, reorder="reorder" in chosen, reload="reload" in chosen
    )
    if "unknown_first" in chosen:
        found = replace(found, unknown_first=_NOT_VERIFICATION["unknown_first"])
    if "retry_first" in chosen:
        found = replace(found, retry_first=lambda _request: True)
    return found


# A reordered late observation can be the last thing a closing run waits for. The shell delivers
# the clock after every pass that changed state, and core drains the run on that tick, so the
# drive must too: before, it stopped with the run `closing` and nothing left to wait for.
@PURE
@example(chosen=Outcomes(turns=("unparsable",)), flags=frozenset({"retry_first", "reorder"}))
@example(chosen=Outcomes(turns=("unparsable",)), flags=frozenset(_FAULT_FLAGS))
@given(chosen=outcomes, flags=st.sets(st.sampled_from(_FAULT_FLAGS)).map(frozenset))
def test_c4_any_combination_of_delivery_faults_ends_the_run_as_the_plain_run_does(
    chosen: Outcomes, flags: frozenset[str]
) -> None:
    plain = _run(chosen, Faults())
    faulty = _run(chosen, _combined(flags))
    assert faulty.finished == plain.finished
    assert faulty.core.run.result == plain.core.run.result
    assert _decisions(faulty) == _decisions(plain)
