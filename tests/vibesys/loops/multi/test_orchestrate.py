"""Pure agent-run policy, record, and issue-board assertions."""

from __future__ import annotations

import json
from typing import Literal

import pytest

from vibesys.agent_run import issue_board
from vibesys.agent_run.evidence import (
    _detect_plateau,
    _pareto_archive_conflict,
    _pareto_archive_dominators,
    _pareto_archive_summary,
    _pareto_frontier_records,
    _provisional_candidates_since_official,
    _select_final_candidate,
    _terminal_workspace_notice,
    _trusted_candidate_records,
)
from vibesys.agent_run.options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.evaluators.validation_recipe import (
    ValidationRecipe,
    ValidationRecipeArtifact,
)
from vibesys.prompts import PROMPTS_DIR
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.roles.profiler import ProfilerSummary
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    OrchestratorPlan,
)
from vibesys.search.hypothesis import HypothesisConfig, HypothesisSearch
from vibesys.search.hypothesis import cadence as _cadence
from vs_loop_state.api import RoundRecord

_THROUGHPUT_LATENCY = MetricSpace(
    objectives=(
        Objective(name="throughput", direction="max"),
        Objective(name="latency", direction="min"),
    )
)


def _official_evaluation_reason(  # noqa: PLR0913
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
        memory_layout="directories",
        operator_constraints=("Preserve ordering",),
        metric_space=MetricSpace(),
    )
    descriptor = descriptor_from_options(options, orchestration_id="single-agent")
    assert descriptor.id == "single-agent"
    assert descriptor.config_version == 1
    assert descriptor.options == {
        "interface": "service",
        "max_rounds": 7,
        "max_retries_per_round": 4,
        "judge_every": 2,
        "official_eval_every": 5,
        "memory_layout": "directories",
        "modality": "messages",
        "operator_constraints": ["Preserve ordering"],
        "metric_space": MetricSpace().model_dump(mode="json"),
        "profile_guided": None,
    }


def test_validation_recipe_rejects_non_workspace_inputs():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="workspace-relative"):
        ValidationRecipe(
            name="focused-tests",
            command="uv run pytest -q tests/entrypoints/test_server.py",
            input_paths=["../outside.py"],
            purpose="Exercise the local server contract.",
        )


def test_validation_recipe_artifact_rejects_invented_top_level_shape():  # noqa: ANN201  # tracked: #288
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


def test_issue_board_publishes_authoritative_validation_recipe_schema(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"

    path = issue_board.write_validation_recipe_schema(progress)
    schema = json.loads(path.read_text())

    assert path == progress / "validation" / "recipe-schema.json"
    assert schema["properties"]["version"]["const"] == 1
    assert schema["properties"]["recipes"]["minItems"] == 1
    assert schema["properties"]["recipes"]["maxItems"] == 8
    assert schema["examples"][0]["recipes"][0]["name"] == "focused-tests"


def test_pre_round_decision_accepts_booleans():  # noqa: ANN201  # tracked: #288
    d = PreRoundDecision(need_profile=True, profile_focus="decode kernels", reasoning="ok")
    assert d.need_profile is True
    assert d.profile_focus == "decode kernels"


def test_orchestrator_plan_revert_round_optional():  # noqa: ANN201  # tracked: #288
    p = OrchestratorPlan(
        task="redo",
        pass_criteria="passes tests",  # noqa: S106  # tracked: #288
        revert_to_round=3,
        reasoning="step back",
    )
    assert p.revert_to_round == 3


def test_official_evaluation_cadence_counts_candidate_checkpoints_not_rounds():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(1, "a", None, None, False, reviewed=False, hypothesis_outcome="continue"),  # noqa: FBT003  # tracked: #288
        RoundRecord(2, "b", None, None, True, reviewed=True, hypothesis_outcome="proven"),  # noqa: FBT003  # tracked: #288
        RoundRecord(3, "c", None, None, False, reviewed=True, hypothesis_outcome="rejected"),  # noqa: FBT003  # tracked: #288
        RoundRecord(4, "d", None, None, True, reviewed=True, hypothesis_outcome="proven"),  # noqa: FBT003  # tracked: #288
    ]

    assert _provisional_candidates_since_official(records) == 2
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


def test_frontier_candidate_forces_review_outside_sparse_cadence():  # noqa: ANN201  # tracked: #288
    assert _review_due(
        round_number=5,
        max_rounds=20,
        judge_every=3,
        outcome=HypothesisOutcome.DISPROVEN,
        candidate_evidence_fresh=True,
    )


def test_measured_candidate_forces_review_even_when_disposition_is_downgraded():  # noqa: ANN201  # tracked: #288
    assert _review_due(
        round_number=5,
        max_rounds=20,
        judge_every=3,
        outcome=HypothesisOutcome.INCONCLUSIVE,
        candidate_evidence_fresh=True,
    )


def test_reused_candidate_evidence_does_not_bypass_sparse_review():  # noqa: ANN201  # tracked: #288
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


def test_changed_candidate_row_is_fresh_even_when_artifact_name_is_reused():  # noqa: ANN201  # tracked: #288
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


def test_official_evaluation_cadence_counts_reviewed_frontier_tradeoff():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(
            2,
            "b",
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_outcome="disproven",
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={"throughput": 120.0, "latency": 90.0},
        )
    ]

    assert _provisional_candidates_since_official(records) == 1


def test_typed_unknown_retention_is_not_trusted_as_pareto_state():  # noqa: ANN201  # tracked: #288
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

    assert _pareto_frontier_records([record], _THROUGHPUT_LATENCY) == []


def test_noise_aware_dominance_preserves_sub_noise_alternatives():  # noqa: ANN201  # tracked: #288
    space = MetricSpace(objectives=_THROUGHPUT_LATENCY.objectives, relative_noise=0.03)

    assert not space.dominates(
        {"throughput": 102.0, "latency": 101.0},
        {"throughput": 100.0, "latency": 100.0},
    )
    assert space.dominates(
        {"throughput": 110.0, "latency": 101.0},
        {"throughput": 100.0, "latency": 100.0},
    )


def test_pareto_frontier_keeps_throughput_latency_tradeoff_and_drops_dominated_point():  # noqa: ANN201  # tracked: #288

    def candidate(round_number: int, throughput: float, latency: float) -> RoundRecord:
        return RoundRecord(
            round_number,
            str(round_number) * 40,
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={"throughput": throughput, "latency": latency},
            candidate_evaluation_artifact=f"round-{round_number}.json",
            candidate_operating_point="concurrency=128",
        )

    latency_parent = candidate(1, 100.0, 80.0)
    throughput_parent = candidate(2, 140.0, 100.0)
    dominated = candidate(3, 90.0, 110.0)

    frontier = _pareto_frontier_records(
        [latency_parent, throughput_parent, dominated],
        _THROUGHPUT_LATENCY,
    )

    assert [record.round_number for record in frontier] == [1, 2]


def test_live_archive_rejects_stale_frontier_claim_for_dominated_candidate():  # noqa: ANN201  # tracked: #288
    trusted = RoundRecord(
        61,
        "a" * 40,
        None,
        None,
        True,  # noqa: FBT003  # tracked: #288
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 8795.8, "latency": 7724.0},
    )

    conflict = _pareto_archive_conflict(
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_metrics={"throughput": 7258.5, "latency": 9601.6},
        records=[trusted],
        space=_THROUGHPUT_LATENCY,
    )

    assert conflict is not None
    assert "round 61" in conflict
    assert "frozen into the hypothesis plan" in conflict


def test_live_archive_preserves_real_throughput_latency_tradeoff():  # noqa: ANN201  # tracked: #288
    trusted = RoundRecord(
        6,
        "a" * 40,
        None,
        None,
        True,  # noqa: FBT003  # tracked: #288
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 100.0, "latency": 80.0},
    )

    assert (
        _pareto_archive_conflict(
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
            candidate_metrics={"throughput": 140.0, "latency": 100.0},
            records=[trusted],
            space=_THROUGHPUT_LATENCY,
        )
        is None
    )


def test_pareto_archive_distinguishes_trusted_and_pending_candidates():  # noqa: ANN201  # tracked: #288
    trusted = RoundRecord(
        49,
        "a" * 40,
        None,
        None,
        True,  # noqa: FBT003  # tracked: #288
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 5307.2, "latency": 3289.7},
        candidate_evaluation_artifact="h31.json",
        candidate_operating_point="concurrency=128",
    )
    pending = RoundRecord(
        51,
        "b" * 40,
        None,
        None,
        False,  # noqa: FBT003  # tracked: #288
        reviewed=False,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 6827.7, "latency": 3628.7},
        candidate_evaluation_artifact="h33.json",
        candidate_operating_point="concurrency=192",
        candidate_retention_reason="higher-throughput tradeoff",
    )

    summary = _pareto_archive_summary([trusted, pending], _THROUGHPUT_LATENCY)

    assert "Trusted frontier parents" in summary
    assert "round 49" in summary
    # The pending row is listed separately and told apart by *why* it is not a
    # trusted parent. Both reasons are named, because after #535 a row can land
    # here for failing review or for carrying an implementer-reported number.
    assert "not yet usable as trusted parents" in summary
    assert "independent review" in summary
    assert "implementer's own report" in summary
    assert "round 51" in summary


def test_pareto_archive_summary_bounds_pending_claims_with_an_omission_notice():  # noqa: ANN201  # tracked: #288
    """The newest pending claims remain visible and older ones are disclosed."""
    pending_records = [
        RoundRecord(
            round_number,
            chr(ord("a") + round_number) * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
            reviewed=False,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={
                "throughput": 6000.0 + round_number,
                "latency": 3000.0 + round_number,
            },
            candidate_evaluation_artifact=f"h{round_number}.json",
            candidate_operating_point="concurrency=192",
            candidate_retention_reason="higher-throughput tradeoff",
        )
        for round_number in range(1, 11)
    ]

    summary = _pareto_archive_summary(pending_records, _THROUGHPUT_LATENCY)
    assert summary == _pareto_archive_summary(list(reversed(pending_records)), _THROUGHPUT_LATENCY)

    for record in pending_records[-8:]:
        assert record.commit is not None
        assert f"round {record.round_number}, commit {record.commit[:12]}" in summary
    assert "round 1, commit bbbbbbbbbbbb" not in summary
    assert "round 2, commit cccccccccccc" not in summary
    assert "2 older untrusted claims omitted from this context (rounds 1-2)" in summary
    assert "do not treat any omitted claim as a trusted parent" in summary


def test_pareto_archive_summary_omission_notice_agrees_with_its_own_count():  # noqa: ANN201  # tracked: #288
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
            False,  # noqa: FBT003  # tracked: #288
            reviewed=False,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={
                "throughput": 6000.0 + round_number,
                "latency": 3000.0 + round_number,
            },
            candidate_evaluation_artifact=f"h{round_number}.json",
            candidate_operating_point="concurrency=192",
            candidate_retention_reason="higher-throughput tradeoff",
        )
        # Nine records against a limit of eight omits exactly one.
        for round_number in range(1, 10)
    ]

    summary = _pareto_archive_summary(pending_records, _THROUGHPUT_LATENCY)
    assert "1 older untrusted claim omitted from this context (round 1)" in summary
    assert "claims omitted" not in summary
    assert "rounds 1-1" not in summary


def test_pareto_archive_summary_lists_all_pending_claims_within_the_limit():  # noqa: ANN201  # tracked: #288
    """A short pending list needs no omission notice."""
    pending_records = [
        RoundRecord(
            round_number,
            chr(ord("a") + round_number) * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
            reviewed=False,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={
                "throughput": 6000.0 + round_number,
                "latency": 3000.0 + round_number,
            },
        )
        for round_number in range(1, 9)
    ]

    summary = _pareto_archive_summary(pending_records, _THROUGHPUT_LATENCY)

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
        True,  # noqa: FBT003  # tracked: #288
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


def test_implementer_report_cannot_seed_archive_or_dominate_candidates():  # noqa: ANN201  # tracked: #288
    """Regression for #535: implementer provenance never becomes a trusted parent.

    A reviewed, accuracy-passing implementer self-report that persisted
    ``candidate_retained=True`` must not be selected by
    ``_trusted_candidate_records`` and must not count as a dominator in a later
    Pareto decision. A framework-provenance row of the same shape still does.
    """
    space = MetricSpace(objectives=(Objective(name="accuracy", direction="max"),))
    implementer = _accuracy_row(1, 0.95, provenance="implementer")
    framework = _accuracy_row(2, 0.95, provenance="framework")

    # Trusted Pareto-parent selection is gated on framework provenance.
    assert _trusted_candidate_records([implementer], space) == []
    assert _trusted_candidate_records([framework], space) == [framework]
    # A weaker later candidate is only dominated by the trusted framework row,
    # never by the untrusted implementer self-report.
    weaker = {"accuracy": 0.80}
    assert _pareto_archive_dominators(weaker, [implementer], space) == []
    assert _pareto_archive_dominators(weaker, [framework], space) == [framework]


def _official_record(  # noqa: PLR0913  # test record builder
    round_number: int,
    commit: str,
    perf: float,
    *,
    retained: bool = True,
    passed: bool = True,
    provenance: Literal["framework", "implementer"] = "framework",
    metrics: dict[str, float] | None = None,
) -> RoundRecord:
    """Build one reviewed record eligible for final selection when trusted."""
    return RoundRecord(
        round_number,
        commit,
        perf,
        "throughput",
        passed,
        reviewed=True,
        judge_verdict="pass" if passed else "fail",
        metrics=metrics or {},
        official_evaluation=True,
        candidate_retained=retained,
        perf_direction="max",
        perf_provenance=provenance,
    )


def test_final_candidate_is_noise_aware_and_rejects_untrusted_records():  # noqa: ANN201  # tracked: #288
    space = MetricSpace(relative_noise=0.05)
    older = _official_record(1, "a" * 40, 100.0)
    newer_within_noise = _official_record(2, "b" * 40, 103.0)
    self_reported = _official_record(3, "c" * 40, 500.0, provenance="implementer")
    rejected = _official_record(4, "d" * 40, 600.0, retained=False)
    failed = _official_record(5, "e" * 40, 700.0, passed=False)

    assert (
        _select_final_candidate([older, newer_within_noise, self_reported, rejected, failed], space)
        is newer_within_noise
    )


def test_final_pareto_candidate_requires_canonical_official_metrics():  # noqa: ANN201  # tracked: #288
    official = _official_record(
        1,
        "a" * 40,
        100.0,
        metrics={"throughput": 100.0, "latency": 10.0},
    )
    provisional = RoundRecord(
        2,
        "b" * 40,
        None,
        None,
        True,  # noqa: FBT003  # tracked: #288
        reviewed=True,
        judge_verdict="pass",
        candidate_metrics={"throughput": 200.0, "latency": 5.0},
        candidate_retained=True,
    )

    assert _select_final_candidate([official, provisional], _THROUGHPUT_LATENCY) is official


def test_official_evaluation_cadence_resets_at_verified_checkpoint():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(
            1,
            "a",
            10.0,
            "tok/s",
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_outcome="proven",
            official_evaluation=True,
            official_evaluation_reason="orchestrator_request",
        ),
        RoundRecord(2, "b", None, None, True, reviewed=True, hypothesis_outcome="proven"),  # noqa: FBT003  # tracked: #288
    ]

    assert _provisional_candidates_since_official(records) == 1
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


def test_terminal_workspace_notice_points_designer_to_hypothesis_parent():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(28, "a" * 40, None, None, False),  # noqa: FBT003  # tracked: #288
        RoundRecord(
            29,
            "b" * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_id="bad-scheduler",
            hypothesis_outcome="rejected",
        ),
        RoundRecord(
            30,
            "c" * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
            reviewed=False,
            hypothesis_id="bad-scheduler",
            hypothesis_outcome="disproven",
            hypothesis_parent_round=28,
        ),
    ]

    notice = _terminal_workspace_notice(records)

    assert notice is not None
    assert "workspace edits are still present" in notice
    assert "recorded pre-hypothesis parent is round 28" in notice
    assert "revert_to_round=28" in notice


def test_terminal_workspace_notice_preserves_pareto_tradeoff_commit():  # noqa: ANN201  # tracked: #288
    record = RoundRecord(
        51,
        "b" * 40,
        None,
        None,
        False,  # noqa: FBT003  # tracked: #288
        reviewed=False,
        hypothesis_id="capacity-192",
        hypothesis_outcome="disproven",
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 6827.7, "latency": 3628.7},
    )

    notice = _terminal_workspace_notice([record])

    assert notice is not None
    assert "Preserve commit" in notice
    assert "awaiting independent review" in notice
    assert "do not erase a credible throughput/latency tradeoff" in notice


def test_terminal_workspace_notice_preserves_credible_continuation_checkpoint():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(28, "a" * 40, None, None, False),  # noqa: FBT003  # tracked: #288
        RoundRecord(
            34,
            "b" * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
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
            False,  # noqa: FBT003  # tracked: #288
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
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_id="host-autopsy",
            hypothesis_outcome="disproven",
            hypothesis_parent_round=28,
        ),
    ]

    notice = _terminal_workspace_notice(records)

    assert notice is not None
    assert "recorded pre-hypothesis parent is round 28" in notice
    assert "most recent earlier nonterminal checkpoint is round 35" in notice
    assert "preserve that checkpoint instead of discarding prior gains" in notice
    assert "An older implementation cannot be required to reproduce" in notice


def test_terminal_workspace_notice_keeps_original_parent_after_same_id_reproposal():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(60, "a" * 40, None, None, True),  # noqa: FBT003  # tracked: #288
        RoundRecord(
            61,
            "b" * 40,
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
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
            True,  # noqa: FBT003  # tracked: #288
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
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_id="quantum-decode",
            hypothesis_outcome="inconclusive",
            hypothesis_parent_round=62,
        ),
    ]

    notice = _terminal_workspace_notice(records)

    assert notice is not None
    assert "recorded pre-hypothesis parent is round 60" in notice
    assert "revert_to_round=60" in notice
    assert "recorded pre-hypothesis parent is round 62" not in notice


def test_profiler_summary_perf_metric_optional():  # noqa: ANN201  # tracked: #288
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


def test_progress_writes_orchestrator_plan(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress.md"
    plan = OrchestratorPlan(
        task="Build FastAPI server",
        pass_criteria="/health returns 200",  # noqa: S106  # tracked: #288
        reasoning="Round 1 cold start",
        expected_effect="Forecast 1.3x to 1.6x throughput",
        minimum_acceptance_criteria="Retain at >=1.15x with no latency regression",
    )
    issue_board.append_orchestrator_plan(progress, 1, plan)
    text = progress.read_text()
    assert "Round 1 — Orchestrator (plan)" in text
    assert "Build FastAPI server" in text
    assert "/health returns 200" in text
    assert "Forecast 1.3x to 1.6x throughput" in text
    assert "Retain at >=1.15x with no latency regression" in text


@pytest.mark.parametrize(
    ("progress_name", "artifact_root"),
    [("progress", "progress"), ("progress.md", "progress-artifacts")],
)
def test_progress_writes_typed_role_handoffs_atomically(tmp_path, progress_name, artifact_root):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / progress_name
    plan = OrchestratorPlan(
        hypothesis_id="transport-boundary",
        task="Replace the request-local queue.",
        pass_criteria="The direct path activates.",  # noqa: S106  # tracked: #288
        reasoning="The retained profile leaves a service residual.",
    )
    implementation = ImplementerResponse(
        summary="Implemented direct delivery.",
        expected_behavior="No request-local queue wakeup.",
        evidence="Untrusted implementer claim.",
    )

    plan_path = issue_board.write_plan_artifact(progress, 12, plan)
    evidence_path = issue_board.write_implementer_artifact(progress, 12, 2, implementation)

    assert plan_path == tmp_path / artifact_root / "plans" / "round-0012.json"
    assert evidence_path == (
        tmp_path / artifact_root / "evidence" / "round-0012-attempt-02-implementer.json"
    )
    assert OrchestratorPlan.model_validate_json(plan_path.read_text()) == plan
    assert ImplementerResponse.model_validate_json(evidence_path.read_text()) == implementation
    assert not list((tmp_path / artifact_root).rglob(".*.tmp*"))


def test_persisted_implementer_attempts_define_resume_boundary(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"
    implementation = ImplementerResponse(
        summary="Retained the first target run.",
        expected_behavior="A resumed round must not overwrite it.",
    )
    first = issue_board.write_implementer_artifact(progress, 8, 1, implementation)
    second = issue_board.write_implementer_artifact(progress, 8, 2, implementation)

    assert issue_board.implementer_artifact_paths(progress, 8) == [first, second]
    assert issue_board.next_implementer_attempt(progress, 8) == 3
    assert issue_board.next_implementer_attempt(progress, 9) == 1


def test_implementer_start_marker_advances_the_resume_boundary(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"
    implementation = ImplementerResponse(
        summary="Recorded after the marker.",
        expected_behavior="A killed attempt must not be replayed under its label.",
    )
    marker = issue_board.write_implementer_start_marker(progress, 8, 1)

    assert marker == (
        tmp_path / "progress" / "evidence" / "round-0008-attempt-01-implementer.started.json"
    )
    # An attempt killed mid-invoke leaves the marker and no completed artifact,
    # yet the round must resume on attempt 2.
    assert issue_board.implementer_artifact_paths(progress, 8) == []
    assert issue_board.next_implementer_attempt(progress, 8) == 2

    completed = issue_board.write_implementer_artifact(progress, 8, 1, implementation)

    # The marker and its own completed artifact name one attempt, not two.
    assert issue_board.implementer_artifact_paths(progress, 8) == [completed]
    assert issue_board.next_implementer_attempt(progress, 8) == 2

    issue_board.write_implementer_start_marker(progress, 9, 1)

    assert issue_board.next_implementer_attempt(progress, 8) == 2
    assert issue_board.next_implementer_attempt(progress, 9) == 2


def test_agent_memory_paths_distinguish_files_from_directories(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    workspace = tmp_path / "workspace"
    directory = workspace / "progress"
    artifact = directory / "plans" / "round-0012.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n")

    assert issue_board.display_path(directory, workspace) == "progress/"
    assert issue_board.display_path(artifact, workspace) == "progress/plans/round-0012.json"


def test_progress_replaces_interrupted_stage_instead_of_duplicating_it(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"
    issue_board.append_pre_round_decision(
        progress,
        7,
        PreRoundDecision(
            need_profile=True,
            profile_focus="stale focus",
            reasoning="stale decision",
        ),
    )
    issue_board.append_orchestrator_plan(
        progress,
        7,
        OrchestratorPlan(
            task="Keep this plan",
            pass_criteria="plan remains",  # noqa: S106  # tracked: #288
            reasoning="retained plan",
        ),
    )

    issue_board.append_pre_round_decision(
        progress,
        7,
        PreRoundDecision(
            need_profile=False,
            profile_focus="",
            reasoning="resumed decision",
        ),
    )

    text = (progress / "round-0007.md").read_text()
    assert text.count("## Round 7 — Orchestrator (pre-round)") == 1
    assert "resumed decision" in text
    assert "stale decision" not in text
    assert text.count("## Round 7 — Orchestrator (plan)") == 1
    assert "Keep this plan" in text


def test_progress_replacement_preserves_operator_recovery_section(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"
    issue_board.append_hypothesis_continuation(
        progress,
        7,
        plan=OrchestratorPlan(
            hypothesis_id="transport",
            hypothesis="remove queue fanout",
            task="stale initial implementation task",
            pass_criteria="source is recoverable",  # noqa: S106  # tracked: #288
            reasoning="continue interrupted work",
        ),
        started_round=6,
        continuation_step="recover exact source",
    )
    round_file = progress / "round-0007.md"
    with round_file.open("a") as document:
        document.write(
            "## Operator recovery evidence\n"
            "Exact measured bytes are retained at `recovery/source.py`.\n\n"
        )

    issue_board.append_hypothesis_continuation(
        progress,
        7,
        plan=OrchestratorPlan(
            hypothesis_id="transport",
            hypothesis="remove queue fanout",
            task="stale initial implementation task",
            pass_criteria="source is recoverable",  # noqa: S106  # tracked: #288
            reasoning="resume interrupted work",
        ),
        started_round=6,
        continuation_step="verify recovered source",
    )

    text = round_file.read_text()
    assert text.count("## Round 7 — Active hypothesis continuation") == 1
    assert "### Current continuation delta" in text
    assert "verify recovered source" in text
    assert "recover exact source" not in text
    assert "stale initial implementation task" not in text
    assert text.count("## Operator recovery evidence") == 1
    assert "Exact measured bytes are retained" in text


def test_progress_preserves_distinct_attempts_but_replaces_same_attempt(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress.md"
    issue_board.append_implementer(
        progress,
        3,
        1,
        ImplementerResponse(summary="interrupted", expected_behavior="old"),
    )
    issue_board.append_implementer(
        progress,
        3,
        1,
        ImplementerResponse(summary="resumed", expected_behavior="new"),
    )
    issue_board.append_implementer(
        progress,
        3,
        2,
        ImplementerResponse(summary="retry", expected_behavior="newer"),
    )

    text = progress.read_text()
    assert text.count("## Round 3 — Implementer (attempt 1)") == 1
    assert text.count("## Round 3 — Implementer (attempt 2)") == 1
    assert "interrupted" not in text
    assert "resumed" in text
    assert "retry" in text


def test_progress_writes_profiler_summary_with_perf(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress.md"
    summary = ProfilerSummary(
        analysis="launch-bound",
        bottlenecks="attention kernel 40%",
        suggestions="swap to flashinfer",
        perf_metric=8.2,
        perf_unit="req/s",
    )
    issue_board.append_profiler_summary(progress, 2, summary)
    text = progress.read_text()
    assert "Round 2 — Profiler" in text
    assert "perf_metric**: 8.2 req/s" in text
    assert "flashinfer" in text


def test_progress_append_implementer_and_judge(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress.md"
    issue_board.append_implementer(
        progress,
        3,
        1,
        ImplementerResponse(summary="added cuda graph", expected_behavior="replay works"),
    )
    issue_board.append_judge(
        progress,
        3,
        1,
        JudgeResponse(analysis="good", feedback="", verdict=Verdict.PASS),
    )
    text = progress.read_text()
    assert "Round 3 — Implementer (attempt 1)" in text
    assert "Round 3 — Judge (attempt 1)" in text
    assert "verdict**: pass" in text


def test_directory_memory_layout_splits_rounds_and_bounds_reads(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    roadmap, progress = issue_board.resolve_paths(tmp_path, "directories")
    issue_board.ensure_roadmap_file(roadmap)
    for round_number in range(1, 16):
        issue_board.append_pre_round_decision(
            progress,
            round_number,
            PreRoundDecision(
                need_profile=False,
                profile_focus="",
                reasoning=f"decision-{round_number}",
            ),
        )

    assert (roadmap / "index.md").exists()
    assert (progress / "round-0001.md").exists()
    assert (progress / "round-0015.md").exists()
    recent = issue_board.read_progress(progress)
    assert "## Round 11 —" not in recent
    assert "## Round 12 —" in recent
    assert "## Round 15 —" in recent


def test_ensure_roadmap_seeds_header_when_missing(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.agent_run import issue_board  # noqa: PLC0415  # tracked: #288

    p = tmp_path / "roadmap.md"
    assert not p.exists()
    issue_board.ensure_roadmap_file(p)
    assert p.exists()
    text = p.read_text()
    # The seed must scaffold the four sections so the orchestrator's first
    # round starts with a clear structure.
    assert "## Major" in text
    assert "## Minor" in text
    assert "## Done" in text
    assert "## Abandoned" in text


def test_ensure_roadmap_does_not_overwrite_existing(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.agent_run import issue_board  # noqa: PLC0415  # tracked: #288

    p = tmp_path / "roadmap.md"
    p.write_text("# my custom plan\n")
    issue_board.ensure_roadmap_file(p)
    assert p.read_text() == "# my custom plan\n"


def test_read_roadmap_returns_text(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.agent_run import issue_board  # noqa: PLC0415  # tracked: #288

    p = tmp_path / "roadmap.md"
    p.write_text("hello\n")
    assert issue_board.read_roadmap(p) == "hello\n"


def test_read_roadmap_missing_returns_empty(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.agent_run import issue_board  # noqa: PLC0415  # tracked: #288

    p = tmp_path / "nope.md"
    assert issue_board.read_roadmap(p) == ""


def test_outer_prompts_reference_memory_paths_without_embedding_contents():  # noqa: ANN201  # tracked: #288
    template_dir = PROMPTS_DIR / "loops" / "multi"
    single_template_dir = PROMPTS_DIR / "loops" / "single"
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
        role_dir = single_template_dir if name == "single_agent_round_prompt.j2" else template_dir
        role_prompt = (role_dir / name).read_text()
        assert "pareto_archive_location" in role_prompt
        assert "pareto_archive_summary" not in role_prompt


@pytest.mark.parametrize(
    ("progress_name", "expected"),
    (("progress.md", "pareto-frontier.md"), ("progress", "progress/pareto-frontier.md")),  # noqa: PT007  # tracked: #288
)
def test_pareto_archive_is_materialized_beside_progress(tmp_path, progress_name, expected):  # noqa: ANN001, ANN201  # tracked: #288
    progress_path = tmp_path / progress_name

    document = issue_board.write_pareto_archive(progress_path, "Trusted frontier: round 4")

    assert document == tmp_path / expected
    assert document.read_text() == "# Pareto frontier\n\nTrusted frontier: round 4\n"


def _record(round_number: int, perf: float | None, unit: str = "tok/s"):  # noqa: ANN202  # tracked: #288
    """Build a RoundRecord shorthand for plateau tests."""
    return RoundRecord(
        round_number=round_number,
        commit=f"sha{round_number:03d}",
        perf_metric=perf,
        perf_unit=unit if perf is not None else None,
        passed=perf is not None,
        official_evaluation=perf is not None,
        official_evaluation_reason="cadence" if perf is not None else None,
    )


def test_detect_plateau_returns_none_when_too_few_rounds():  # noqa: ANN201  # tracked: #288

    # Two rounds is below the 3-round minimum streak.
    records = [_record(1, 40.0), _record(2, 41.0)]
    assert _detect_plateau(records) is None


def test_detect_plateau_fires_on_flat_perf_streak():  # noqa: ANN201  # tracked: #288

    # 41.0 vs 41.5 is ~1.2% spread — well under the 5% threshold.
    records = [_record(1, 41.0), _record(2, 41.5), _record(3, 41.2)]
    warning = _detect_plateau(records)
    assert warning is not None
    assert "rounds 1–3" in warning  # noqa: RUF001  # tracked: #288
    assert "tok/s" in warning


def test_detect_plateau_skips_when_perf_diverges():  # noqa: ANN201  # tracked: #288

    # 41.0 vs 116.0 is ~64% spread — clearly off-plateau.
    records = [_record(1, 41.0), _record(2, 116.0), _record(3, 114.5)]
    assert _detect_plateau(records) is None


def test_detect_plateau_ignores_rounds_without_perf():  # noqa: ANN201  # tracked: #288
    """Rounds where the profiler skipped or the round failed (perf=None) must
    not interrupt the streak — only valid measurements count."""

    records = [
        _record(1, 41.0),
        _record(2, None),  # profiler skipped or failed round
        _record(3, 41.3),
        _record(4, 41.1),
    ]
    warning = _detect_plateau(records)
    assert warning is not None
    assert "rounds 1–4" in warning  # noqa: RUF001  # tracked: #288


def test_detect_plateau_ignores_failed_official_measurements():  # noqa: ANN201  # tracked: #288
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
    warning = _detect_plateau(records)
    assert warning is not None
    assert "rounds 1–4" in warning  # noqa: RUF001  # tracked: #288


def test_failed_official_measurement_cannot_complete_plateau_streak():  # noqa: ANN201  # tracked: #288

    failed = _record(3, 41.1)
    failed.passed = False
    assert _detect_plateau([_record(1, 41.0), _record(2, 41.2), failed]) is None


def test_detect_plateau_streak_must_be_recent():  # noqa: ANN201  # tracked: #288
    """A plateau early in the run that's followed by a clear win must NOT
    fire a warning on the next round — only the *last N* matter."""

    records = [
        _record(1, 41.0),  # plateau
        _record(2, 41.2),  # plateau
        _record(3, 41.1),  # plateau (would fire here)
        _record(4, 116.0),  # break
    ]
    # By round 4, the recent streak (rounds 2,3,4) spans 41.2-116.0 → no plateau.
    assert _detect_plateau(records) is None
