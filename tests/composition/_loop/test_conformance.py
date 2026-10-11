"""The conformance suite (design note 9.2), pure tier: C1, C3, C4, C5, C6, C7, C8, C9.

Every test is parametrized by the registered subjects (`subjects.SUBJECTS`) and draws its
scripted world from the subject, so each strategy on the core lifecycle runs the same
properties. The crash tier (C2) is `test_conformance_crash.py`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.composition._loop import conformance as c
from tests.composition._loop.subjects import SUBJECTS

from vs_core.api import Step
from vs_core.testing.drive import Faults
from vs_core.testing.liveness import Budget, End, assert_live
from vs_runtime.api.core import REQUEST_DISPATCH
from vs_runtime.api.testing import HostOptions

SUBJECT_NAMES = sorted(SUBJECTS)
PURE = settings(
    max_examples=12,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

outcomes = st.builds(
    c.Outcomes,
    measurements=st.lists(st.sampled_from(c.MEASUREMENT_OUTCOMES), max_size=3).map(tuple),
    turns=st.lists(st.sampled_from(c.TURN_OUTCOMES), max_size=4).map(tuple),
    adoption_fails=st.booleans(),
)


@pytest.mark.parametrize("name", SUBJECT_NAMES)
@PURE
@given(data=st.data())
def test_c1_c3_c7_a_run_of_any_world_is_deterministic_replayable_and_persistable(
    name: str, data: st.DataObject
) -> None:
    subject = SUBJECTS[name]
    world = c.Faulted(data.draw(subject.worlds)(), data.draw(outcomes))
    recorded = c.run_subject(subject, world)
    c.check_decide_is_deterministic(subject, recorded)
    c.check_a_replayed_proposal_is_deduplicated(subject, recorded)
    c.check_states_round_trip_the_codec(subject, recorded)
    c.check_a_folded_event_twice_changes_nothing(subject, recorded)
    c.check_limits_hold(subject, recorded)
    c.check_adoption_is_safe(recorded)


DELIVERY_FAULTS = {
    "unknown_first": Faults(unknown_first=lambda _request: True),
    "retry_first": Faults(retry_first=lambda _request: True),
    "duplicate": Faults(duplicate=True),
    "reorder": Faults(reorder=True),
    "reload": Faults(reload=True),
    "all": Faults(
        unknown_first=lambda _request: True,
        retry_first=lambda _request: True,
        duplicate=True,
        reorder=True,
        reload=True,
    ),
}


@pytest.mark.parametrize("name", SUBJECT_NAMES)
@pytest.mark.parametrize("fault", sorted(DELIVERY_FAULTS))
@PURE
@given(data=st.data())
def test_c4_delivery_faults_never_change_the_decisions_or_the_liveness(
    name: str, fault: str, data: st.DataObject
) -> None:
    """Unknown, retried, duplicated, reordered and reloaded observations decide the same."""
    subject = SUBJECTS[name]
    make = data.draw(subject.worlds)
    # Agent turns are lost, so the pure driver stops where the first turn would be answered.
    quiet = c.Outcomes(measurements=("workload_failure",), turns=(c.LOST_TURN,))
    plain = c.run_pure(subject, c.Faulted(make(), quiet), Faults())
    faulty = c.run_pure(subject, c.Faulted(make(), quiet), DELIVERY_FAULTS[fault])
    assert faulty == plain


@pytest.mark.parametrize("name", SUBJECT_NAMES)
@PURE
@given(data=st.data(), stop_at=st.integers(min_value=0, max_value=40))
def test_c5_an_operator_stop_at_any_request_ends_the_run_and_leaves_nothing(
    name: str, data: st.DataObject, stop_at: int
) -> None:
    """A stop injected while the ``stop_at``-th request runs ends the run, resumably or not."""
    # A run that stays open (a request nothing answers) ends at its deadline; keep that short.
    subject = replace(SUBJECTS[name], harness=replace(SUBJECTS[name].harness, deadline_at=15_000.0))
    world = c.Faulted(data.draw(subject.worlds)(), data.draw(outcomes), stop_at=stop_at)
    recorded = c.run_subject(subject, world, host=HostOptions(attach=world.attach), live=False)
    core = recorded.run.core
    if not world.stopped:
        return
    budget = Budget(retries=subject.harness.limits.max_retries)
    if recorded.run.stopped:
        # The host gave up on a request core cannot cancel; the run stays open for a resume.
        assert_live(recorded.run.journal, core, budget, End.STOPPED)
        return
    # The run may have decided its own result in the same step the stop arrived; whatever it
    # is, the run is terminal with every attempt settled and nothing open.
    assert core.run.result is not None
    assert_live(recorded.run.journal, core, budget, End.TERMINAL)


@pytest.mark.parametrize("name", SUBJECT_NAMES)
def test_c10_a_request_blocked_in_any_turn_step_ends_the_attempt_that_has_one_in_flight(
    name: str,
) -> None:
    """The subject's shortest run meets both steps with a request in flight, so the probe bites."""
    subject = SUBJECTS[name]
    met: set[Step] = set()
    # The shortest run first; a subject whose shortest run ends before an attempt starts needs
    # its fuller default run.
    for world in (subject.smallest_world, subject.default_world):
        if {Step.RENDERING, Step.AWAITING} <= met:
            break
        recorded = c.run_subject(subject, world(), live=False)
        met |= c.check_a_blocked_request_ends_the_track_that_has_one_in_flight(subject, recorded)
    assert {Step.RENDERING, Step.AWAITING} <= met


@pytest.mark.parametrize("name", SUBJECT_NAMES)
def test_c9_every_request_kind_is_classified(name: str) -> None:
    """A kind core knows is either issued by the strategy, issued only to recover, or unused."""
    subject = SUBJECTS[name]
    known = {request.model_fields["kind"].default for request in REQUEST_DISPATCH}
    assert subject.issued <= known
    assert known >= c.RECOVERY_ONLY
    assert not subject.issued & c.RECOVERY_ONLY


def _kind_cases() -> list[object]:
    return [
        pytest.param(name, kind, how, id=f"{name}-{kind}-{how}")
        for name in SUBJECT_NAMES
        for kind in sorted(SUBJECTS[name].issued)
        for how in c.KIND_FAULTS
        if (kind, how) in c.CONCLUSIVE_AFTER_FAULT
    ]


@pytest.mark.parametrize(("name", "kind", "how"), _kind_cases())
def test_c9_a_kind_answered_unknown_retryably_or_never_ends_live_without_repeating_an_effect(
    name: str, kind: str, how: str
) -> None:
    subject = SUBJECTS[name]
    harness = replace(subject.harness, deadline_at=15_000.0)
    faulted = replace(subject, harness=harness)
    world = c.KindFault(subject.default_world(), kind, how)
    c.run_subject(faulted, world)
