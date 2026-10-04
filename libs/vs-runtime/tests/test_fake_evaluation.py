"""Contract of the scriptable trusted-evaluation Fake."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import (
    ContentDigest,
    EvaluationDependencyError,
    EvaluationPending,
    EvaluationRequest,
    EvaluationStep,
    EvidenceFingerprints,
    OwnedEvaluationDependencies,
    ServiceEvaluationSettlements,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements
from vs_runtime.api import (
    BenchmarkEvaluation,
    CandidateProfile,
    CandidateProfileStatus,
    member_workspace_id,
)
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

    evaluation = FakeEvaluation(profiling_supported=True)
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


@given(script=st.lists(st.sampled_from(["observed", "unsupported", "failed"]), max_size=3))
def test_a_fake_that_cannot_profile_reports_every_profile_unsupported(script: list[str]) -> None:
    """Like a production executor without profile evidence, scripts cannot make it capable."""
    evaluation = FakeEvaluation()
    evaluation.script_profile(
        *(
            CandidateProfile(
                revision="scripted",
                status=CandidateProfileStatus(kind),
                diagnosis=None if kind == "failed" else "diagnosis",
                failure="turn failed" if kind == "failed" else None,
            )
            for kind in script
        )
    )

    assert asyncio.run(evaluation.can_profile()) is False
    profile = asyncio.run(evaluation.profile("rev", "q", member_id="p"))
    assert profile.status is CandidateProfileStatus.UNSUPPORTED
    assert profile.capture_started is False
    assert profile.revision == "rev"
    assert asyncio.run(FakeEvaluation(profiling_supported=True).can_profile()) is True


@given(requester_count=st.integers(min_value=2, max_value=5), injected=st.booleans())
def test_requester_cancellation_preserves_other_waits(
    requester_count: int, *, injected: bool
) -> None:
    async def scenario() -> None:
        settlements = FakeEvaluationSettlements()
        evaluation = FakeEvaluation(
            settlement_observations=(
                ServiceEvaluationSettlements(settlements.backend, settlements.namespace)
                if injected
                else settlements
            ),
            association_cancellation=settlements.cancel_association if injected else None,
            scope_release=settlements.release_scope if injected else None,
            scope_reopen=settlements.reopen_scope if injected else None,
        )
        digest = ContentDigest.sha256(b"shared capture")
        fingerprints = EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        )
        scopes = tuple(f"scope-{index}" for index in range(requester_count))
        handles = tuple(
            [
                await settlements.submit(
                    EvaluationRequest(
                        key="shared",
                        owner_scope=scope,
                        stages=(EvaluationStep(name="benchmark", payload={}),),
                    ),
                    fingerprints,
                )
                for scope in scopes
            ]
        )
        assert len(set(handles)) == 1
        handle = handles[0]
        evaluation.submitted_generations = {(scope, handle): 0 for scope in scopes}
        report = await settlements.coordinator.recorded_snapshot(handle)
        evaluation.submitted_reports[handle] = report.model_dump_json()
        for scope in scopes[:-1]:
            await evaluation.cancel_submitted(handle, scope_id=scope)
            await evaluation.cancel_submitted(handle, scope_id=scope)
            assert (
                await evaluation.submitted_report(handle, scope_id=scope)
                == report.model_dump_json()
            )
        assert settlements.executor.cancellations == []
        (observation,) = await settlements.observe(
            OwnedEvaluationDependencies(scope_id=scopes[-1], generation=0, handles=(handle,))
        )
        assert isinstance(observation.result, EvaluationPending)
        await evaluation.cancel_submitted(handle, scope_id=scopes[-1])
        assert settlements.executor.cancellations == [handle]

    asyncio.run(scenario())


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [False, True])
async def test_release_reopen_keeps_history_and_advances_requester_generation(
    *, injected: bool
) -> None:
    settlements = FakeEvaluationSettlements()
    evaluation = FakeEvaluation(
        settlement_observations=(
            ServiceEvaluationSettlements(settlements.backend, settlements.namespace)
            if injected
            else settlements
        ),
        association_cancellation=settlements.cancel_association if injected else None,
        scope_release=settlements.release_scope if injected else None,
        scope_reopen=settlements.reopen_scope if injected else None,
    )
    digest = ContentDigest.sha256(b"reopened shared capture")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    scopes = tuple(member_workspace_id(member) for member in ("owner", "requester"))
    handles = [
        await settlements.submit(
            EvaluationRequest(
                key="shared",
                owner_scope=scope,
                stages=(EvaluationStep(name="benchmark", payload={}),),
            ),
            fingerprints,
        )
        for scope in scopes
    ]
    assert handles[0] == handles[1]
    handle = handles[0]
    evaluation.submitted_generations = {(scope, handle): 0 for scope in scopes}
    report = await settlements.coordinator.recorded_snapshot(handle)
    evaluation.submitted_reports[handle] = report.model_dump_json()
    release = await evaluation.release_jobs("requester")
    assert release.first_release
    assert release.evaluations == ()
    assert settlements.executor.cancellations == []
    assert not (await evaluation.release_jobs("requester")).first_release
    await evaluation.reopen_jobs("requester")
    assert await evaluation.submitted_generation(handle, scope_id=scopes[1]) == 0
    assert await evaluation.submitted_report(handle, scope_id=scopes[1]) == report.model_dump_json()
    old = OwnedEvaluationDependencies(scope_id=scopes[1], generation=0, handles=(handle,))
    with pytest.raises(EvaluationDependencyError):
        await evaluation.settlements().observe(old)
    joined = await settlements.submit(
        EvaluationRequest(
            key="shared", owner_scope=scopes[1], owner_generation=1, stages=report.request.stages
        ),
        fingerprints,
    )
    assert joined == handle
    evaluation.submitted_generations[scopes[1], handle] = 1
    assert await evaluation.submitted_generation(handle, scope_id=scopes[1]) == 1
    with pytest.raises(EvaluationDependencyError):
        await evaluation.settlements().observe(old)
    current = old.model_copy(update={"generation": 1})
    assert isinstance(
        (await evaluation.settlements().observe(current))[0].result, EvaluationPending
    )
