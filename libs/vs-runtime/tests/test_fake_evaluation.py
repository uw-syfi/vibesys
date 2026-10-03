"""Contract of the scriptable trusted-evaluation Fake."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api import BenchmarkEvaluation, CandidateProfile, CandidateProfileStatus
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces


class _ScriptedFailureError(RuntimeError):
    """Synthetic evaluator failure."""


def _reading(value: float) -> BenchmarkEvaluation:
    return BenchmarkEvaluation(executed=True, metric_name="m", metric_value=value)


_SCRIPT = st.lists(st.one_of(st.floats(0, 100), st.none()), max_size=6)


@given(script=_SCRIPT)
def test_scripted_benchmarks_return_or_raise_in_call_order(script: list[float | None]) -> None:
    evaluation = FakeEvaluation()
    evaluation.script_benchmark(
        *(_ScriptedFailureError() if value is None else _reading(value) for value in script)
    )

    async def drain() -> list[float | None]:
        observed: list[float | None] = []
        for _ in script:
            try:
                observed.append((await evaluation.benchmark(FakeWorkspace())).metric_value)
            except _ScriptedFailureError:
                observed.append(None)
        return observed

    assert asyncio.run(drain()) == script
    assert asyncio.run(evaluation.benchmark(FakeWorkspace())) == evaluation.default_benchmark


def test_root_benchmarks_use_their_own_script_whatever_the_interleaving() -> None:
    evaluation = FakeEvaluation()
    evaluation.script_root_benchmark(_reading(1.0))
    evaluation.script_benchmark(_reading(2.0))
    candidate = FakeWorkspace(workspace_id="candidate")

    async def scenario() -> tuple[float | None, float | None]:
        first = await evaluation.benchmark(candidate)
        root = await evaluation.benchmark(FakeWorkspace())
        return first.metric_value, root.metric_value

    assert asyncio.run(scenario()) == (2.0, 1.0)


def test_gate_holds_one_call_until_released() -> None:
    evaluation = FakeEvaluation()
    evaluation.script_benchmark(_reading(1.0), _reading(2.0))
    gate = evaluation.gate("benchmark", 1)

    async def scenario() -> list[float | None]:
        finished: list[float | None] = []

        async def call() -> None:
            finished.append((await evaluation.benchmark(FakeWorkspace())).metric_value)

        first = asyncio.ensure_future(call())
        second = asyncio.ensure_future(call())
        await first
        await gate.entered.wait()
        assert not second.done()
        assert not gate.finished
        gate.release()
        await second
        return finished

    assert asyncio.run(scenario()) == [1.0, 2.0]
    assert gate.finished
    assert not gate.cancelled_while_live


@pytest.mark.parametrize("discard_first", [False, True], ids=["live", "discarded"])
def test_gate_notes_whether_a_cancellation_found_the_workspace_live(*, discard_first: bool) -> None:
    evaluation = FakeEvaluation()
    gate = evaluation.gate("accuracy", 0)
    workspaces = FakeWorkspaces(FakeWorkspace(), supports_parallel_candidates=True)

    async def scenario() -> None:
        candidate = await workspaces.create_candidate()
        held = asyncio.ensure_future(evaluation.accuracy(candidate))
        await gate.entered.wait()
        if discard_first:
            await candidate.discard()
        held.cancel()
        with pytest.raises(asyncio.CancelledError):
            await held

    asyncio.run(scenario())
    assert gate.finished
    assert gate.cancelled_while_live is not discard_first


def test_a_call_is_gated_at_most_once() -> None:
    evaluation = FakeEvaluation()
    evaluation.gate("accuracy", 0)
    with pytest.raises(ValueError, match="already gated"):
        evaluation.gate("accuracy", 0)


@given(script=st.lists(st.sampled_from(["observed", "unsupported", "failed", "raise"]), max_size=5))
def test_scripted_profiles_describe_the_requested_revision_in_call_order(
    script: list[str],
) -> None:
    def outcome(kind: str) -> CandidateProfile | BaseException:
        if kind == "raise":
            return _ScriptedFailureError()
        status = CandidateProfileStatus(kind)
        failed = status is CandidateProfileStatus.FAILED
        return CandidateProfile(
            revision="scripted",
            status=status,
            diagnosis=None if failed else "diagnosis",
            failure="turn failed" if failed else None,
        )

    evaluation = FakeEvaluation()
    evaluation.script_profile(*(outcome(kind) for kind in script))

    async def drain() -> list[str]:
        observed: list[str] = []
        for index, _ in enumerate(script):
            try:
                profile = await evaluation.profile(f"rev{index}", "q", member_id="p")
            except _ScriptedFailureError:
                observed.append("raise")
                continue
            assert profile.revision == f"rev{index}"
            observed.append(profile.status.value)
        return observed

    assert asyncio.run(drain()) == script
    unscripted = asyncio.run(evaluation.profile("rev", "q", member_id="p"))
    assert unscripted.status is CandidateProfileStatus.FAILED
    assert [call.revision for call in evaluation.profile_calls] == [
        *(f"rev{index}" for index in range(len(script))),
        "rev",
    ]
