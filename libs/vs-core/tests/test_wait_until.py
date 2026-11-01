"""A wait states how long it may take; ``decide`` ends it at that limit or when a watch fires.

Properties over the pure core and no real time: the run clock moves by ``reach``, which records
what the runner loop records once time reaches a moment (``vs_core.testing.limits``).
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.testing.limits import reach

from .wait_support import (
    RUN_DEADLINE,
    dispatch_all,
    failures,
    fresh,
    open_records,
    operations,
    submit,
    wait,
)

LIMITS = st.floats(min_value=0.5, max_value=200.0, allow_nan=False)


@given(limit=LIMITS, later=st.floats(min_value=0.0, max_value=50.0, allow_nan=False))
def test_a_wait_past_its_limit_ends_with_timed_out(limit: float, later: float) -> None:
    state, codec = fresh()
    state = dispatch_all(submit(state, wait(codec, "w", core.Within(limit=limit))).state)
    (record,) = operations(state)
    assert record.until.at == pytest.approx(limit)

    just_before = reach(state, limit * 0.99)
    assert failures(just_before) == []
    assert [r.phase for r in open_records(just_before.state)] != []

    ended = reach(state, limit + later)
    (failure,) = failures(ended)
    assert failure.reason is core.FailureReason.TIMED_OUT
    assert failure.decision == core.DecisionId(root="w")
    assert failure.limit_at == pytest.approx(limit)
    (done,) = operations(ended.state)
    assert done.phase is core.RequestPhase.COMPLETED
    assert done.failure == failure
    assert failures(reach(ended.state, limit + later + 1_000.0)) == []


@given(limits=st.lists(LIMITS, min_size=1, max_size=5))
def test_no_wait_stays_open_past_the_latest_limit(limits: list[float]) -> None:
    state, codec = fresh()
    waits = [wait(codec, f"w{i}", core.Within(limit=limit)) for i, limit in enumerate(limits)]
    state = dispatch_all(submit(state, *waits).state)
    latest = max(r.until.at for r in operations(state))
    assert latest == pytest.approx(max(limits))
    # Time passes in steps; at the latest limit nothing canonical is still open.
    for moment in sorted({*(r.until.at for r in operations(state)), latest}):
        state = reach(state, moment).state
    assert open_records(state) == []


@given(limit=st.floats(min_value=RUN_DEADLINE, max_value=RUN_DEADLINE * 3, allow_nan=False))
def test_a_limit_is_capped_at_the_run_deadline(limit: float) -> None:
    state, codec = fresh()
    state = submit(state, wait(codec, "w", core.Within(limit=limit))).state
    (record,) = operations(state)
    assert record.until.at == RUN_DEADLINE


@given(limit=st.floats(max_value=0.0, allow_nan=False, allow_infinity=False))
def test_a_non_positive_limit_is_refused(limit: float) -> None:
    state, codec = fresh()
    result = submit(state, wait(codec, "w", core.Within(limit=limit)))
    assert operations(result.state) == []
    rejected = [e for e in result.events if isinstance(e, core.Rejected)]
    assert [e.decision_id for e in rejected] == [core.DecisionId(root="w")]


def _closed_workspace() -> core.WhileOpen:
    """A workspace core has no open attempt for: its watch has already fired."""
    return core.WhileOpen(
        workspace=core.AttemptRef(attempt_id=core.AttemptId(root="never-opened"), generation=0)
    )


@given(
    declared=st.lists(st.booleans(), min_size=1, max_size=6),
    limit=LIMITS,
)
def test_a_fired_watch_ends_exactly_the_waits_that_declared_it(
    declared: list[bool], limit: float
) -> None:
    state, codec = fresh()
    waits = [
        wait(
            codec,
            f"w{i}",
            core.Within(limit=limit, watches=(_closed_workspace(),) if watched else ()),
        )
        for i, watched in enumerate(declared)
    ]
    result = reach(submit(state, *waits).state, 1.0)
    ended = {f.decision: f for f in failures(result)}
    expected = {core.DecisionId(root=f"w{i}") for i, watched in enumerate(declared) if watched}
    assert set(ended) == expected
    assert all(f.reason is core.FailureReason.WORKSPACE_CLOSED for f in ended.values())
    assert all(f.watch == _closed_workspace() for f in ended.values())
    still_open = {r.request.decision_id for r in open_records(result.state)}
    assert still_open == {core.DecisionId(root=f"w{i}") for i in range(len(declared))} - expected


def test_a_watch_alone_is_not_an_until() -> None:
    _, codec = fresh()
    work = wait(codec, "w", core.Within(limit=10.0)).request
    bad = core.Wait.model_construct(request=work, until=_closed_workspace())
    with pytest.raises(core.ContractError, match="until"):
        core.check_decisions([bad])


def _with_open_workspace(state: core.CoreState) -> tuple[core.CoreState, core.AttemptRef]:
    view = core.AttemptView(
        attempt_id=core.AttemptId(root="open"),
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=core.DecisionId(root="admission"),
    )
    opened = state.model_copy(update={"attempts": core.AttemptsState(attempts=(view,))})
    return opened, core.AttemptRef(attempt_id=view.attempt_id, generation=0)


@given(limit=LIMITS)
def test_a_watch_whose_condition_holds_does_not_end_the_wait(limit: float) -> None:
    state, codec = fresh()
    state, workspace = _with_open_workspace(state)
    watches = (
        core.WhileOpen(workspace=workspace),
        core.WhileAlive(job=core.ResourceId(root="job-not-yet-registered")),
    )
    result = submit(state, wait(codec, "w", core.Within(limit=limit) | watches[0] | watches[1]))
    assert failures(result) == []
    (record,) = open_records(result.state)
    assert record.until.watches == watches
