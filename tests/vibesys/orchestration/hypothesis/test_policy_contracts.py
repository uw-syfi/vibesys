"""Pure hypothesis policy, record, and issue-board assertions."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest
from tests.support import make_orchestrator_plan

from vibesys.hypothesis import HypothesisConfig, HypothesisSearch
from vibesys.hypothesis import cadence as _cadence
from vibesys.hypothesis.history import CandidateDisposition, HypothesisOutcome, RoundRecord
from vibesys.hypothesis.transitions import (
    detect_plateau,
    pareto_archive_dominators,
    pareto_frontier_records,
    provisional_candidates_since_official,
    select_final_candidate,
    trusted_candidate_records,
)
from vibesys.metrics import MetricComparison, MetricSpace, Objective
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.multi.contracts import ImplementerResponse, PreRoundDecision
from vibesys.orchestration.multi.prompts import PROMPT_DIR as MULTI_PROMPT_DIR
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.single.prompts import PROMPT_DIR as SINGLE_PROMPT_DIR
from vibesys.prompts import PROMPTS_DIR, render_template
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import (
    ValidationRecipe,
    ValidationRecipeArtifact,
)

_THROUGHPUT_LATENCY = MetricSpace(
    objectives=(
        Objective(name="throughput", direction="max"),
        Objective(name="latency", direction="min"),
    )
)

_SEARCH = HypothesisSearch(HypothesisConfig(max_rounds=10))
_SHARED_PROMPTS = PROMPTS_DIR / "shared"


def pareto_archive_summary(records: list[RoundRecord], space: MetricSpace) -> str:
    """Render the archive body the way the plugins' Pareto document does."""
    archive = _SEARCH.archive_view(records, space=space)
    return str(
        render_template("_notices/pareto_archive.j2", template_dir=_SHARED_PROMPTS, archive=archive)
    )


def pareto_archive_conflict(
    *,
    candidate_disposition: CandidateDisposition,
    candidate_metrics: dict[str, float],
    records: list[RoundRecord],
    space: MetricSpace,
) -> str | None:
    """Render the archive conflict the way the judge prompt does."""
    conflict = _SEARCH.pareto_conflict(
        disposition=candidate_disposition, metrics=candidate_metrics, records=records, space=space
    )
    if conflict is None:
        return None
    return str(
        render_template(
            "_notices/archive_conflict.j2",
            template_dir=_SHARED_PROMPTS,
            pareto_archive_conflict=conflict,
        )
    )


def terminal_workspace_notice(records: list[RoundRecord]) -> str | None:
    """Render the resumed regression notice the way a consumer template does."""
    notice = _SEARCH.initial_carry(records).regression
    if notice is None:
        return None
    return str(
        render_template(
            "_notices/regression.j2", template_dir=_SHARED_PROMPTS, regression_info=notice
        )
    )


def _official_evaluation_reason(  # noqa: PLR0913  # LW-040136 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    *,
    records: list[RoundRecord],
    round_number: int,
    max_rounds: int,
    official_eval_every: int,
    requested: bool,
    candidate_ready: bool,
) -> str | None:
    search = HypothesisSearch(
        HypothesisConfig(max_rounds=max_rounds, official_eval_every=official_eval_every)
    )
    return search.official_due(
        records=records,
        round_number=round_number,
        requested=requested,
        candidate_ready=candidate_ready,
    )


def _review_due(
    *,
    round_number: int,
    max_rounds: int,
    judge_every: int,
    outcome: HypothesisOutcome,
    candidate_evidence_fresh: bool = False,
) -> bool:
    search = HypothesisSearch(HypothesisConfig(max_rounds=max_rounds, judge_every=judge_every))
    return search.review_due(
        round_number=round_number,
        outcome=outcome,
        candidate_evidence_is_fresh=candidate_evidence_fresh,
    )


def _candidate_evidence_is_fresh(
    implementation: ImplementerResponse, records: list[RoundRecord]
) -> bool:
    return _cadence.candidate_evidence_fresh(
        candidate_metrics=implementation.candidate_metrics,
        candidate_evaluation_artifact=implementation.candidate_evaluation_artifact,
        records=records,
    )


def test_orchestration_descriptor_contains_only_policy_settings() -> None:
    options = AgentOrchestrationOptions(
        interface="service",
        modality="messages",
        max_rounds=7,
        max_retries_per_round=4,
        judge_every=2,
        official_eval_every=5,
        operator_constraints=("Preserve ordering",),
        metric_space=MetricSpace(),
    )
    descriptor = OrchestrationDescriptor(
        id="single-agent",
        config_version=1,
        options=options.model_dump(mode="json"),
    )
    assert descriptor.id == "single-agent"
    assert descriptor.config_version == 1
    assert descriptor.options == {
        "interface": "service",
        "max_rounds": 7,
        "max_retries_per_round": 4,
        "judge_every": 2,
        "official_eval_every": 5,
        "modality": "messages",
        "operator_constraints": ["Preserve ordering"],
        "metric_space": MetricSpace().model_dump(mode="json"),
        "profile_guided": None,
    }


def test_validation_recipe_rejects_non_workspace_inputs() -> None:
    with pytest.raises(ValueError, match="workspace-relative"):
        ValidationRecipe(
            name="focused-tests",
            command="uv run pytest -q tests/entrypoints/test_server.py",
            input_paths=["../outside.py"],
            purpose="Exercise the local server contract.",
        )


def test_validation_recipe_artifact_rejects_invented_top_level_shape() -> None:
    with pytest.raises(ValueError, match="recipes"):
        ValidationRecipeArtifact.model_validate(
            {
                "version": 1,
                "checks": [
                    {
                        "name": "focused-tests",
                        "command": "uv run pytest -q test_server.py",
                    }
                ],
            }
        )


def test_pre_round_decision_accepts_booleans() -> None:
    d = PreRoundDecision(need_profile=True, profile_focus="decode kernels", reasoning="ok")
    assert d.need_profile is True
    assert d.profile_focus == "decode kernels"


def test_orchestrator_plan_revert_round_optional() -> None:
    p = make_orchestrator_plan(
        task="redo",
        criteria="passes tests",
        revert_to_round=3,
        reasoning="step back",
    )
    assert p.revert_to_round == 3


def test_official_evaluation_cadence_counts_candidate_checkpoints_not_rounds() -> None:
    records = [
        RoundRecord(
            1, "a", None, None, passed=False, reviewed=False, hypothesis_outcome="continue"
        ),
        RoundRecord(2, "b", None, None, passed=True, reviewed=True, hypothesis_outcome="proven"),
        RoundRecord(3, "c", None, None, passed=False, reviewed=True, hypothesis_outcome="rejected"),
        RoundRecord(4, "d", None, None, passed=True, reviewed=True, hypothesis_outcome="proven"),
    ]

    assert provisional_candidates_since_official(records) == 2
    assert (
        _official_evaluation_reason(
            records=records,
            round_number=5,
            max_rounds=20,
            official_eval_every=3,
            requested=False,
            candidate_ready=True,
        )
        == "cadence"
    )


def test_frontier_candidate_forces_review_outside_sparse_cadence() -> None:
    assert _review_due(
        round_number=5,
        max_rounds=20,
        judge_every=3,
        outcome=HypothesisOutcome.DISPROVEN,
        candidate_evidence_fresh=True,
    )


def test_measured_candidate_forces_review_even_when_disposition_is_downgraded() -> None:
    assert _review_due(
        round_number=5,
        max_rounds=20,
        judge_every=3,
        outcome=HypothesisOutcome.INCONCLUSIVE,
        candidate_evidence_fresh=True,
    )


def test_reused_candidate_evidence_does_not_bypass_sparse_review() -> None:
    metrics = {"throughput": 6205.0, "latency": 10660.0}
    record = RoundRecord(
        round_number=75,
        commit="a" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=False,
        candidate_disposition=CandidateDisposition.PREREQUISITE.value,
        candidate_metrics=metrics,
        candidate_evaluation_artifact="h37-round77-controller-raw.json",
        candidate_operating_point="concurrency 512",
    )
    implementation = ImplementerResponse(
        summary="Rebuilt derived reports without running a benchmark.",
        expected_behavior="The retained raw row is unchanged.",
        hypothesis_outcome=HypothesisOutcome.INCONCLUSIVE,
        candidate_disposition=CandidateDisposition.PREREQUISITE,
        candidate_metrics=metrics,
        candidate_evaluation_artifact="h37-round77-controller-raw.json",
        candidate_operating_point="concurrency 512",
    )

    assert not _candidate_evidence_is_fresh(implementation, [record])
    assert not _review_due(
        round_number=76,
        max_rounds=200,
        judge_every=3,
        outcome=implementation.hypothesis_outcome,
        candidate_evidence_fresh=False,
    )


def test_changed_candidate_row_is_fresh_even_when_artifact_name_is_reused() -> None:
    record = RoundRecord(
        round_number=4,
        commit="a" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=False,
        candidate_metrics={"throughput": 100.0},
        candidate_evaluation_artifact="candidate.json",
        candidate_operating_point="load 8",
    )
    implementation = ImplementerResponse(
        summary="Measured a changed row.",
        expected_behavior="Throughput changes.",
        candidate_metrics={"throughput": 110.0},
        candidate_evaluation_artifact="candidate.json",
        candidate_operating_point="load 8",
    )

    assert _candidate_evidence_is_fresh(implementation, [record])


def test_official_evaluation_cadence_counts_reviewed_frontier_tradeoff() -> None:
    records = [
        RoundRecord(
            2,
            "b",
            None,
            None,
            passed=True,
            reviewed=True,
            hypothesis_outcome="disproven",
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={"throughput": 120.0, "latency": 90.0},
            candidate_retained=True,
        )
    ]

    assert provisional_candidates_since_official(records) == 1


def test_typed_unknown_retention_is_not_trusted_as_pareto_state() -> None:
    record = RoundRecord(
        round_number=2,
        commit="b" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        reviewed=True,
        hypothesis_id="qualitative-result",
        hypothesis_declared_outcome="supported",
        judge_verdict="pass",
        hypothesis_outcome="proven",
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 120.0, "latency": 90.0},
        candidate_retained=None,
    )

    assert pareto_frontier_records([record], _THROUGHPUT_LATENCY) == []


def test_noise_aware_dominance_preserves_sub_noise_alternatives() -> None:
    space = MetricSpace(objectives=_THROUGHPUT_LATENCY.objectives, relative_noise=0.03)

    assert not space.dominates(
        {"throughput": 102.0, "latency": 101.0},
        {"throughput": 100.0, "latency": 100.0},
    )
    assert space.dominates(
        {"throughput": 110.0, "latency": 101.0},
        {"throughput": 100.0, "latency": 100.0},
    )


def test_pareto_frontier_keeps_throughput_latency_tradeoff_and_drops_dominated_point() -> None:

    def candidate(round_number: int, throughput: float, latency: float) -> RoundRecord:
        return RoundRecord(
            round_number,
            str(round_number) * 40,
            None,
            None,
            passed=True,
            reviewed=True,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={"throughput": throughput, "latency": latency},
            candidate_evaluation_artifact=f"round-{round_number}.json",
            candidate_operating_point="concurrency=128",
            candidate_retained=True,
            perf_provenance="framework",
        )

    latency_parent = candidate(1, 100.0, 80.0)
    throughput_parent = candidate(2, 140.0, 100.0)
    dominated = candidate(3, 90.0, 110.0)

    frontier = pareto_frontier_records(
        [latency_parent, throughput_parent, dominated],
        _THROUGHPUT_LATENCY,
    )

    assert [record.round_number for record in frontier] == [1, 2]


def test_live_archive_rejects_stale_frontier_claim_for_dominated_candidate() -> None:
    trusted = RoundRecord(
        61,
        "a" * 40,
        None,
        None,
        passed=True,
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 8795.8, "latency": 7724.0},
        candidate_retained=True,
        perf_provenance="framework",
    )

    conflict = pareto_archive_conflict(
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_metrics={"throughput": 7258.5, "latency": 9601.6},
        records=[trusted],
        space=_THROUGHPUT_LATENCY,
    )

    assert conflict is not None
    assert "round 61" in conflict
    assert "frozen into the hypothesis plan" in conflict


def test_live_archive_preserves_real_throughput_latency_tradeoff() -> None:
    trusted = RoundRecord(
        6,
        "a" * 40,
        None,
        None,
        passed=True,
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 100.0, "latency": 80.0},
        candidate_retained=True,
        perf_provenance="framework",
    )

    assert (
        pareto_archive_conflict(
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
            candidate_metrics={"throughput": 140.0, "latency": 100.0},
            records=[trusted],
            space=_THROUGHPUT_LATENCY,
        )
        is None
    )


def test_pareto_archive_distinguishes_trusted_and_pending_candidates() -> None:
    trusted = RoundRecord(
        49,
        "a" * 40,
        None,
        None,
        passed=True,
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 5307.2, "latency": 3289.7},
        candidate_evaluation_artifact="h31.json",
        candidate_operating_point="concurrency=128",
        candidate_retained=True,
        perf_provenance="framework",
    )
    pending = RoundRecord(
        51,
        "b" * 40,
        None,
        None,
        passed=False,
        reviewed=False,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 6827.7, "latency": 3628.7},
        candidate_evaluation_artifact="h33.json",
        candidate_operating_point="concurrency=192",
        candidate_retention_reason="higher-throughput tradeoff",
        candidate_retained=True,
    )

    summary = pareto_archive_summary([trusted, pending], _THROUGHPUT_LATENCY)

    assert "Trusted frontier parents" in summary
    assert "round 49" in summary
    # The pending row is listed separately and told apart by *why* it is not a
    # trusted parent. Both reasons are named, because after #535 a row can land
    # here for failing review or for carrying an implementer-reported number.
    assert "not yet usable as trusted parents" in summary
    assert "independent review" in summary
    assert "implementer's own report" in summary
    assert "round 51" in summary


def test_pareto_archive_summary_bounds_pending_claims_with_an_omission_notice() -> None:
    """The newest pending claims remain visible and older ones are disclosed."""
    pending_records = [
        RoundRecord(
            round_number,
            chr(ord("a") + round_number) * 40,
            None,
            None,
            passed=False,
            reviewed=False,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={
                "throughput": 6000.0 + round_number,
                "latency": 3000.0 + round_number,
            },
            candidate_evaluation_artifact=f"h{round_number}.json",
            candidate_operating_point="concurrency=192",
            candidate_retention_reason="higher-throughput tradeoff",
            candidate_retained=True,
        )
        for round_number in range(1, 11)
    ]

    summary = pareto_archive_summary(pending_records, _THROUGHPUT_LATENCY)
    assert summary == pareto_archive_summary(list(reversed(pending_records)), _THROUGHPUT_LATENCY)

    for record in pending_records[-8:]:
        assert record.commit is not None
        assert f"round {record.round_number}, commit {record.commit[:12]}" in summary
    assert "round 1, commit bbbbbbbbbbbb" not in summary
    assert "round 2, commit cccccccccccc" not in summary
    assert "2 older untrusted claims omitted from this context (rounds 1-2)" in summary
    assert "do not treat any omitted claim as a trusted parent" in summary


def test_pareto_archive_summary_omission_notice_agrees_with_its_own_count() -> None:
    """One omitted claim reads as singular, and one omitted round is not a range.

    The notice goes into prompt context a model reads, so "1 older untrusted
    claims ... (rounds 3-3)" is not merely untidy: it invites the reader to
    infer a plural set and a span where neither exists.
    """
    pending_records = [
        RoundRecord(
            round_number,
            chr(ord("a") + round_number) * 40,
            None,
            None,
            passed=False,
            reviewed=False,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={
                "throughput": 6000.0 + round_number,
                "latency": 3000.0 + round_number,
            },
            candidate_evaluation_artifact=f"h{round_number}.json",
            candidate_operating_point="concurrency=192",
            candidate_retention_reason="higher-throughput tradeoff",
            candidate_retained=True,
        )
        # Nine records against a limit of eight omits exactly one.
        for round_number in range(1, 10)
    ]

    summary = pareto_archive_summary(pending_records, _THROUGHPUT_LATENCY)
    assert "1 older untrusted claim omitted from this context (round 1)" in summary
    assert "claims omitted" not in summary
    assert "rounds 1-1" not in summary


def test_pareto_archive_summary_lists_all_pending_claims_within_the_limit() -> None:
    """A short pending list needs no omission notice."""
    pending_records = [
        RoundRecord(
            round_number,
            chr(ord("a") + round_number) * 40,
            None,
            None,
            passed=False,
            reviewed=False,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={
                "throughput": 6000.0 + round_number,
                "latency": 3000.0 + round_number,
            },
            candidate_retained=True,
        )
        for round_number in range(1, 9)
    ]

    summary = pareto_archive_summary(pending_records, _THROUGHPUT_LATENCY)

    for record in pending_records:
        assert record.commit is not None
        assert f"round {record.round_number}, commit {record.commit[:12]}" in summary
    assert "untrusted claims omitted" not in summary


def _accuracy_row(
    round_number: int,
    accuracy: float,
    *,
    provenance: Literal["framework", "implementer"],
) -> RoundRecord:
    """A reviewed, accuracy-passing checkpoint with an objective row.

    Models the finding's scenario: an objective-based task with an accuracy
    command but no benchmark result contract, so the headline metric's
    provenance is the only thing distinguishing a trusted framework
    measurement from an implementer self-report.
    """
    return RoundRecord(
        round_number,
        str(round_number) * 40,
        accuracy,
        "accuracy",
        passed=True,
        reviewed=True,
        hypothesis_id="H-acc",
        judge_verdict="pass",
        hypothesis_outcome="proven",
        metrics={"accuracy": accuracy},
        official_evaluation=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_retained=True,
        perf_provenance=provenance,
    )


def test_implementer_report_cannot_seed_archive_or_dominate_candidates() -> None:
    """Regression for #535: implementer provenance never becomes a trusted parent.

    A reviewed, accuracy-passing implementer self-report that persisted
    ``candidate_retained=True`` must not be selected by
    ``trusted_candidate_records`` and must not count as a dominator in a later
    Pareto decision. A framework-provenance row of the same shape still does.
    """
    space = MetricSpace(objectives=(Objective(name="accuracy", direction="max"),))
    implementer = _accuracy_row(1, 0.95, provenance="implementer")
    framework = _accuracy_row(2, 0.95, provenance="framework")

    # Trusted Pareto-parent selection is gated on framework provenance.
    assert trusted_candidate_records([implementer], space) == []
    assert trusted_candidate_records([framework], space) == [framework]
    # A weaker later candidate is only dominated by the trusted framework row,
    # never by the untrusted implementer self-report.
    weaker = {"accuracy": 0.80}
    assert pareto_archive_dominators(weaker, [implementer], space) == []
    assert pareto_archive_dominators(weaker, [framework], space) == [framework]


def _official_record(
    round_number: int,
    perf: float,
    *,
    retained: bool = True,
    passed: bool = True,
    provenance: Literal["framework", "implementer"] = "framework",
) -> RoundRecord:
    """Build one reviewed record eligible for final selection when trusted."""
    return RoundRecord(
        round_number,
        chr(ord("a") + round_number - 1) * 40,
        perf,
        "throughput",
        passed,
        reviewed=True,
        judge_verdict="pass" if passed else "fail",
        metrics={},
        official_evaluation=True,
        candidate_retained=retained,
        perf_direction="max",
        perf_provenance=provenance,
    )


def test_final_candidate_is_noise_aware_and_rejects_untrusted_records() -> None:
    space = MetricSpace(relative_noise=0.05)
    older = _official_record(1, 100.0)
    newer_within_noise = _official_record(2, 103.0)
    self_reported = _official_record(3, 500.0, provenance="implementer")
    rejected = _official_record(4, 600.0, retained=False)
    failed = _official_record(5, 700.0, passed=False)

    assert (
        select_final_candidate([older, newer_within_noise, self_reported, rejected, failed], space)
        is newer_within_noise
    )


def test_final_pareto_candidate_requires_canonical_official_metrics() -> None:
    official = replace(_official_record(1, 100.0), metrics={"throughput": 100.0, "latency": 10.0})
    provisional = RoundRecord(
        2,
        "b" * 40,
        None,
        None,
        passed=True,
        reviewed=True,
        judge_verdict="pass",
        candidate_metrics={"throughput": 200.0, "latency": 5.0},
        candidate_retained=True,
    )

    assert select_final_candidate([official, provisional], _THROUGHPUT_LATENCY) is official


def test_official_evaluation_cadence_resets_at_verified_checkpoint() -> None:
    records = [
        RoundRecord(
            1,
            "a",
            10.0,
            "tok/s",
            passed=True,
            reviewed=True,
            hypothesis_outcome="proven",
            official_evaluation=True,
            official_evaluation_reason="orchestrator_request",
        ),
        RoundRecord(2, "b", None, None, passed=True, reviewed=True, hypothesis_outcome="proven"),
    ]

    assert provisional_candidates_since_official(records) == 1
    assert (
        _official_evaluation_reason(
            records=records,
            round_number=3,
            max_rounds=20,
            official_eval_every=3,
            requested=False,
            candidate_ready=True,
        )
        is None
    )


def test_terminal_workspace_notice_points_designer_to_hypothesis_parent() -> None:
    records = [
        RoundRecord(28, "a" * 40, None, None, passed=False),
        RoundRecord(
            29,
            "b" * 40,
            None,
            None,
            passed=False,
            reviewed=True,
            hypothesis_id="bad-scheduler",
            hypothesis_outcome="rejected",
        ),
        RoundRecord(
            30,
            "c" * 40,
            None,
            None,
            passed=False,
            reviewed=False,
            hypothesis_id="bad-scheduler",
            hypothesis_outcome="disproven",
            hypothesis_parent_round=28,
        ),
    ]

    notice = terminal_workspace_notice(records)

    assert notice is not None
    assert "workspace edits are still present" in notice
    assert "recorded pre-hypothesis parent is round 28" in notice
    assert "revert_to_round=28" in notice


def test_terminal_workspace_notice_preserves_pareto_tradeoff_commit() -> None:
    record = RoundRecord(
        51,
        "b" * 40,
        None,
        None,
        passed=False,
        reviewed=False,
        hypothesis_id="capacity-192",
        hypothesis_outcome="disproven",
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 6827.7, "latency": 3628.7},
        candidate_retained=True,
    )

    notice = terminal_workspace_notice([record])

    assert notice is not None
    assert "Preserve commit" in notice
    assert "awaiting independent review" in notice
    assert "do not erase a credible throughput/latency tradeoff" in notice


def test_terminal_workspace_notice_preserves_credible_continuation_checkpoint() -> None:
    records = [
        RoundRecord(28, "a" * 40, None, None, passed=False),
        RoundRecord(
            34,
            "b" * 40,
            None,
            None,
            passed=False,
            reviewed=False,
            hypothesis_id="host-autopsy",
            hypothesis_outcome="continue",
            hypothesis_parent_round=28,
        ),
        RoundRecord(
            35,
            "c" * 40,
            None,
            None,
            passed=False,
            reviewed=False,
            hypothesis_id="host-autopsy",
            hypothesis_outcome="continue",
            hypothesis_parent_round=28,
        ),
        RoundRecord(
            36,
            "d" * 40,
            None,
            None,
            passed=True,
            reviewed=True,
            hypothesis_id="host-autopsy",
            hypothesis_outcome="disproven",
            hypothesis_parent_round=28,
        ),
    ]

    notice = terminal_workspace_notice(records)

    assert notice is not None
    assert "recorded pre-hypothesis parent is round 28" in notice
    assert "most recent earlier nonterminal checkpoint is round 35" in notice
    assert "preserve that checkpoint instead of discarding prior gains" in notice
    assert "An older implementation cannot be required to reproduce" in notice


def test_terminal_workspace_notice_keeps_original_parent_after_same_id_reproposal() -> None:
    records = [
        RoundRecord(60, "a" * 40, None, None, passed=True),
        RoundRecord(
            61,
            "b" * 40,
            None,
            None,
            passed=True,
            reviewed=True,
            hypothesis_id="quantum-decode",
            hypothesis_outcome="implementation_failed",
            hypothesis_parent_round=60,
        ),
        RoundRecord(
            62,
            "c" * 40,
            None,
            None,
            passed=True,
            reviewed=True,
            hypothesis_id="quantum-decode",
            hypothesis_outcome="blocked",
            hypothesis_parent_round=60,
        ),
        RoundRecord(
            63,
            "d" * 40,
            None,
            None,
            passed=True,
            reviewed=True,
            hypothesis_id="quantum-decode",
            hypothesis_outcome="inconclusive",
            hypothesis_parent_round=62,
        ),
    ]

    notice = terminal_workspace_notice(records)

    assert notice is not None
    assert "recorded pre-hypothesis parent is round 60" in notice
    assert "revert_to_round=60" in notice
    assert "recorded pre-hypothesis parent is round 62" not in notice


def test_profiler_summary_perf_metric_optional() -> None:
    p = ProfilerSummary(analysis="a", bottlenecks="b", suggestions="s")
    assert p.perf_metric is None
    p2 = ProfilerSummary(
        analysis="a",
        bottlenecks="b",
        suggestions="s",
        perf_metric=12.5,
        perf_unit="tok/s",
    )
    assert p2.perf_metric == 12.5
    assert p2.perf_unit == "tok/s"


def test_outer_prompts_reference_memory_paths_without_embedding_contents() -> None:
    template_dir = MULTI_PROMPT_DIR
    plan_prompt = (template_dir / "orchestrator_plan_prompt.j2").read_text()
    pre_prompt = (template_dir / "orchestrator_pre_round_prompt.j2").read_text()

    assert "progress_location" in plan_prompt
    assert "roadmap_location" in plan_prompt
    assert "pareto_archive_location" in plan_prompt
    assert "recent_progress_text" not in plan_prompt
    assert "roadmap_text" not in plan_prompt
    assert "pareto_archive_summary" not in plan_prompt
    assert "Cite" in plan_prompt
    assert "not stable text" in plan_prompt
    assert "under 4,000 output tokens" in plan_prompt
    assert "Once evidence clearly ranks one parent and mechanism" in plan_prompt
    assert "progress_location" in pre_prompt
    assert "recent_progress_text" not in pre_prompt

    for name in ("implementer_prompt.j2", "judge_prompt.j2", "single_agent_round_prompt.j2"):
        role_dir = SINGLE_PROMPT_DIR if name == "single_agent_round_prompt.j2" else template_dir
        role_prompt = (role_dir / name).read_text()
        assert "pareto_archive_location" in role_prompt
        assert "pareto_archive_summary" not in role_prompt


def _record(round_number: int, perf: float | None, unit: str = "tok/s") -> RoundRecord:
    """Build a RoundRecord shorthand for plateau tests."""
    return RoundRecord(
        round_number=round_number,
        commit=f"sha{round_number:03d}",
        perf_metric=perf,
        perf_unit=unit if perf is not None else None,
        passed=perf is not None,
        official_evaluation=perf is not None,
        official_evaluation_reason="cadence" if perf is not None else None,
        judge_verdict="pass" if perf is not None else "deferred",
        perf_comparison=MetricComparison.INCOMPARABLE if perf is not None else None,
        perf_provenance="framework" if perf is not None else None,
    )


def test_detect_plateau_returns_none_when_too_few_rounds() -> None:

    # Two rounds is below the 3-round minimum streak.
    records = [_record(1, 40.0), _record(2, 41.0)]
    assert detect_plateau(records) is None


def test_detect_plateau_fires_on_flat_perf_streak() -> None:

    # 41.0 vs 41.5 is ~1.2% spread — well under the 5% threshold.
    records = [_record(1, 41.0), _record(2, 41.5), _record(3, 41.2)]
    warning = detect_plateau(records)
    assert warning is not None
    assert "rounds 1\u20133" in warning
    assert "tok/s" in warning


def test_detect_plateau_skips_when_perf_diverges() -> None:

    # 41.0 vs 116.0 is ~64% spread — clearly off-plateau.
    records = [_record(1, 41.0), _record(2, 116.0), _record(3, 114.5)]
    assert detect_plateau(records) is None


def test_detect_plateau_ignores_rounds_without_perf() -> None:
    """Rounds where the profiler skipped or the round failed (perf=None) must
    not interrupt the streak — only valid measurements count."""

    records = [
        _record(1, 41.0),
        _record(2, None),  # profiler skipped or failed round
        _record(3, 41.3),
        _record(4, 41.1),
    ]
    warning = detect_plateau(records)
    assert warning is not None
    assert "rounds 1\u20134" in warning


def test_detect_plateau_ignores_failed_official_measurements() -> None:
    """A measured row rejected by the judge or another round gate is not
    trusted trajectory evidence, even when the framework evaluator ran."""

    failed = _record(2, 100.0)
    failed.passed = False
    records = [
        _record(1, 41.0),
        failed,
        _record(3, 41.3),
        _record(4, 41.1),
    ]
    warning = detect_plateau(records)
    assert warning is not None
    assert "rounds 1\u20134" in warning


def test_failed_official_measurement_cannot_complete_plateau_streak() -> None:

    failed = _record(3, 41.1)
    failed.passed = False
    assert detect_plateau([_record(1, 41.0), _record(2, 41.2), failed]) is None


def test_detect_plateau_streak_must_be_recent() -> None:
    """A plateau early in the run that's followed by a clear win must NOT
    fire a warning on the next round — only the *last N* matter."""

    records = [
        _record(1, 41.0),  # plateau
        _record(2, 41.2),  # plateau
        _record(3, 41.1),  # plateau (would fire here)
        _record(4, 116.0),  # break
    ]
    # By round 4, the recent streak (rounds 2,3,4) spans 41.2-116.0 → no plateau.
    assert detect_plateau(records) is None
