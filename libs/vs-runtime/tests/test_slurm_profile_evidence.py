"""A Slurm profile stage's typed statistics reach its evidence as metrics, not summary text."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_evaluation.api import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    SemanticEvaluationStage,
    TrustedEvidence,
)
from vs_evaluation.api.testing import wait_until_executor_started
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_sandbox.api.slurm import (
    GPU_ACTIVE_FRACTION_METRIC,
    HIP_API_TIME_METRIC_PREFIX,
    PROFILE_END,
    PROFILE_STATS_PREFIX,
    HipApiTime,
    ProfileStats,
    split_profile_stats,
)
from vs_sim.api.testing import examples, yield_until

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.asyncio

_HANDLE = "profile-1"


@pytest.fixture(autouse=True)
def _cwd_stays_untouched(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Evaluating a candidate writes only under the stack's root, never the working directory."""
    cwd = tmp_path_factory.mktemp("cwd")
    monkeypatch.chdir(cwd)
    yield
    assert sorted(cwd.iterdir()) == []


_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED})


def _stats() -> st.SearchStrategy[ProfileStats]:
    api = st.builds(
        HipApiTime,
        name=st.text(st.characters(whitelist_categories=("L", "N")), min_size=1, max_size=12),
        total_ms=st.floats(min_value=0, max_value=1e6, allow_nan=False),
        calls=st.integers(min_value=0, max_value=10**6),
    )
    return st.builds(
        ProfileStats,
        gpu_active_fraction=st.none() | st.floats(min_value=0, max_value=1, allow_nan=False),
        top_hip_api=st.lists(api, max_size=5, unique_by=lambda item: item.name).map(tuple),
    )


async def _profile_result(tmp_path: Path, stdout: str) -> EvaluationStepResult:
    cluster = ScenarioCluster()
    cluster.profile_output = stdout
    stack = await build_stack(tmp_path, cluster, profile_command=("python", "capture.py"))
    digest = ContentDigest.sha256(b"same")
    request = EvaluationRequest(
        key="profile",
        stages=(
            EvaluationStep(
                name=EvidenceKind.PROFILE.value,
                payload=SemanticEvaluationStage(
                    snapshot=stack.snapshot,
                    kind=EvidenceKind.PROFILE,
                    fingerprints=EvidenceFingerprints(
                        candidate=digest, evaluator=digest, workload=digest, environment=digest
                    ),
                ).model_dump(mode="json"),
            ),
        ),
    )
    await stack.executor.submit(request, handle_id=_HANDLE)
    await wait_until_executor_started(cluster.accepted, stack.executor, _HANDLE)
    observed = await yield_until(
        lambda: stack.executor.inspect(_HANDLE),
        lambda seen: seen is not None and seen.state in _TERMINAL,
        "a terminal observation",
    )
    assert observed is not None
    (step,) = observed.stage_results
    assert step.result is not None
    await stack.executor.close()
    return step


async def _profile(tmp_path: Path, stdout: str) -> TrustedEvidence:
    step = await _profile_result(tmp_path, stdout)
    return TrustedEvidence.model_validate(step.result)


@settings(max_examples=examples(pr=5, nightly=15), deadline=None)
@given(stats=_stats(), log=st.text(alphabet="abc \n", max_size=300))
async def test_printed_statistics_become_evidence_metrics_and_leave_the_summary(
    stats: ProfileStats, log: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    line = f"{PROFILE_STATS_PREFIX}{stats.model_dump_json()}\n"
    evidence = await _profile(
        tmp_path_factory.mktemp("profile"), f"{log}\nsummary text\n{line}{PROFILE_END}\n"
    )

    assert evidence.outcome is EvidenceOutcome.PASSED
    assert evidence.metrics == stats.metrics()
    assert PROFILE_STATS_PREFIX not in (evidence.semantic_summary or "")
    assert "summary text" in (evidence.semantic_summary or "")
    names = {item.name for item in evidence.metrics}
    assert (GPU_ACTIVE_FRACTION_METRIC in names) == (stats.gpu_active_fraction is not None)
    assert sum(name.startswith(HIP_API_TIME_METRIC_PREFIX) for name in names) == len(
        stats.top_hip_api
    )


async def test_a_capture_without_statistics_is_still_passed_evidence(tmp_path: Path) -> None:
    evidence = await _profile(tmp_path, f"summary text\n{PROFILE_END}\n")
    assert evidence.outcome is EvidenceOutcome.PASSED
    assert evidence.metrics == ()


async def test_profile_keeps_raw_evidence_and_separate_operator_output(
    tmp_path: Path,
) -> None:
    step = await _profile_result(
        tmp_path,
        "API_TOKEN=" + "profile-secret-" * 2_000 + f"\n{PROFILE_END}\n",
    )
    evidence = TrustedEvidence.model_validate(step.result)

    assert evidence.semantic_summary is not None
    assert "profile-secret" in evidence.semantic_summary
    assert len(evidence.semantic_summary) <= MAX_EVIDENCE_SUMMARY_CHARS
    assert step.stdout_tail is not None
    assert "profile-secret" not in step.stdout_tail


async def test_a_malformed_statistics_line_fails_the_profile_naming_the_problem(
    tmp_path: Path,
) -> None:
    bad = json.dumps({"gpu_active_fraction": 7})
    evidence = await _profile(
        tmp_path, f"summary text\n{PROFILE_STATS_PREFIX}{bad}\n{PROFILE_END}\n"
    )
    assert evidence.outcome is EvidenceOutcome.FAILED
    assert "malformed profile statistics line" in (evidence.semantic_summary or "")


@settings(max_examples=examples(pr=5, nightly=15), deadline=None)
@given(stats=_stats(), data=st.data())
async def test_a_capture_cut_anywhere_is_failed_never_passed_with_missing_metrics(
    stats: ProfileStats, data: st.DataObject, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Output that stops before its end line may have lost the statistics: unknown, not zero."""
    whole = f"summary text\n{PROFILE_STATS_PREFIX}{stats.model_dump_json()}\n{PROFILE_END}\n"
    cut = data.draw(st.integers(min_value=0, max_value=len(whole) - 1))
    evidence = await _profile(tmp_path_factory.mktemp("profile"), whole[:cut])

    assert evidence.outcome is EvidenceOutcome.FAILED
    assert "did not arrive whole" in (evidence.semantic_summary or "")
    assert evidence.metrics == ()


@given(stats=_stats(), data=st.data())
def test_split_profile_stats_reads_only_whole_output(
    stats: ProfileStats, data: st.DataObject
) -> None:
    whole = f"summary\n{PROFILE_STATS_PREFIX}{stats.model_dump_json()}\n{PROFILE_END}\n"
    assert split_profile_stats(whole) == ("summary\n", stats)
    cut = data.draw(st.integers(min_value=0, max_value=len(whole) - 1))
    with pytest.raises(ValueError, match="did not arrive whole"):
        split_profile_stats(whole[:cut])
