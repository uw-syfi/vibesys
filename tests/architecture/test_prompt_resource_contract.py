"""Render wheel-owned prompts, without an editable checkout as a resource fallback."""

from __future__ import annotations

import re
import sys
import sysconfig
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from jinja2 import Environment, meta

from vs_core.api import (
    ComparisonPolicy,
    EvidenceId,
    EvidenceKind,
    ObservationStatus,
    ReplyError,
    ReplyErrorCode,
    RevisionId,
    RevisionRef,
)
from vs_sandbox.api.testing import ScriptedDockerCli
from vs_sim.api import OsThreads

if __name__ != "packaged_prompt_contract":
    from tests.support import run_test_command

from vibesys.constants import ComputeBackend
from vibesys.hypothesis import (
    ArchiveConflict,
    ArchiveDominator,
    ExhaustionNotice,
    OrchestratorPlan,
    ParetoArchiveView,
    TerminalWorkspaceEdits,
)
from vibesys.metrics import Objective
from vibesys.orchestration.dynamic.search.api import (
    Blocker,
    BlockerKind,
    CombinePick,
    CombineSource,
    EvaluationQueueView,
    Expectation,
    HandoffNote,
    MainMeasurement,
    MergeableRow,
    MergeOutcome,
    MergeRecord,
    MergeSource,
    PlannerPrompt,
    ReviewEvaluation,
    StrayUnapplied,
)
from vibesys.orchestration.evolve.population import Individual
from vibesys.orchestration.issue_queue.search.api import IssueBrief, PriorPerformance
from vibesys.orchestration.multi.models import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.multi.search.api import (
    GateFacts,
    ImplementerClaims,
    ValidationFailure,
)
from vibesys.orchestration.profilers import (
    ACTIVE_PROFILER_KINDS,
    ProfilerSummary,
    profiler_definition,
    profiler_support_extra,
)
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.skill_selection import (
    PLATFORM_SKELETON,
    PLATFORMS_PARENT,
    platform_skill_excluded_paths,
    resolve_agent_resource_paths,
)
from vibesys.profile_focus import FocusLedger
from vibesys.prompts import core_protocol_variables, render_template
from vibesys.run.contracts import ProfilerKind
from vibesys.run.project_policy import build_project_path_policy
from vibesys.run.workspace_policy import (
    EXCLUDED_WORKSPACE_DIRS,
    build_workspace_materialization_plan,
    skill_copy,
)
from vs_evaluation.api import ProfileField
from vs_issue_tracker.api import Issue, IssueStatus, IssueType
from vs_project.api import Git, Project
from vs_prompts.api import reply_fields, resolve_free_variables
from vs_runtime.api import LocalValidationEvaluation, RunFacts, SkillFact
from vs_runtime.api.infrastructure import (
    ModalEnvironmentFacts,
    ProjectMaterializer,
    SDKRoots,
    discover_skill_dirs,
)
from vs_runtime.api.testing import FakeProjectMaterializationEffects
from vs_sandbox.api import DockerSandbox, HostResourceAccess, host_resource_for_mount
from vs_sim.api import OsProcesses

if TYPE_CHECKING:
    from tests.support.distribution_build import BuiltWheel, PrivatePackagingSource

    from vs_sandbox.api import HostResource


@dataclass(frozen=True)
class _PackagedTree:
    """The installed wheel paired with the exact source snapshot that produced it."""

    source: PrivatePackagingSource
    installed: Path


@pytest.fixture(scope="module")
def packaged_tree(
    tmp_path_factory: pytest.TempPathFactory, built_wheel: BuiltWheel
) -> _PackagedTree:
    """The session's one wheel, unpacked in private scratch."""
    installed = tmp_path_factory.mktemp("prompt-wheel") / "installed"
    with zipfile.ZipFile(built_wheel.wheel) as archive:
        archive.extractall(installed)
    return _PackagedTree(source=built_wheel.source, installed=installed)


def assert_every_source_prompt_is_in_the_wheel(packaged_tree: _PackagedTree) -> None:
    """A forgotten package-data glob must fail even when editable installs render it."""
    source_root = packaged_tree.source.root
    sources = [source_root / "src", *(source_root / "libs").glob("*/src")]
    expected = {
        path.relative_to(source)
        for source in sources
        for path in source.rglob("*")
        if path.is_file() and path.suffix in {".j2", ".md"} and "prompts" in path.parts
    }
    assert expected
    missing = sorted(
        str(path) for path in expected if not (packaged_tree.installed / path).is_file()
    )
    assert not missing, f"Prompts missing from wheel: {missing}"


@pytest.mark.xdist_group("built_wheel")
def test_every_packaged_prompt_renders_without_checkout_fallback(
    packaged_tree: _PackagedTree,
) -> None:
    """Run real rendering in a child whose package imports come from the wheel."""
    assert_every_source_prompt_is_in_the_wheel(packaged_tree)
    script = (
        "import runpy, sys; "
        "sys.path[:0] = sys.argv[1:3]; "
        "module = runpy.run_path(sys.argv[3], run_name='packaged_prompt_contract'); "
        "print(module['render_packaged_prompts'](module['Path'](sys.argv[1])))"
    )
    result = run_test_command(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(packaged_tree.installed),
            sysconfig.get_path("purelib"),
            str(Path(__file__)),
        ],
        cwd=packaged_tree.installed,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def representative_context() -> dict[str, object]:
    """Explicit fixture vocabulary: new template inputs require a deliberate fixture."""
    text_fields = [
        "accelerator_type",
        "accuracy_command",
        "active_component",
        "app_name",
        "baseline",
        "benchmark_command",
        "buildable",
        "candidate_revision",
        "candidate_snapshot_id",
        "constraints",
        "current_round_location",
        "domain_implementer",
        "domain_judge",
        "domain_orchestrator",
        "domain_profiler",
        "domain_single_agent",
        "environment_notes",
        "error",
        "evidence",
        "exhaustion_entry",
        "feedback",
        "framework_revert_commit",
        "gate_approved_evaluation_artifact",
        "gate_approved_perf_unit",
        "gpu",
        "gpu_client",
        "handles",
        "history",
        "history_root",
        "hypothesis",
        "hypothesis_id",
        "implementer_artifact_location",
        "input_partial",
        "interrupted_revision",
        "launcher",
        "load_levels_json",
        "objective",
        "objective_location",
        "official_evaluation_reason",
        "older_ids",
        "outcome",
        "parent_offer_snapshot",
        "parent_revision",
        "pareto_archive_location",
        "pass_criteria",
        "plan_artifact_location",
        "plan_example",
        "plateau_warning",
        "prior_attempt",
        "prior_records_json",
        "prior_revision",
        "profile_focus",
        "profile_name",
        "profiler_entry",
        "profiler_id",
        "profiler_kind",
        "profiler_mcp_name",
        "profiler_support_name",
        "progress_location",
        "question",
        "quoted_hypothesis_id",
        "reason",
        "record_failure",
        "reference_path",
        "regression_entry",
        "request",
        "retained_revision",
        "review",
        "roadmap_location",
        "role",
        "runtime_container_path",
        "runtime_notes",
        "schema",
        "service_command",
        "service_log",
        "session_state_dir",
        "signature",
        "stage_failure",
        "stage_output",
        "status",
        "step",
        "summary",
        "task",
        "user_prompt",
        "validation_location",
        "validation_recipe_contract_location",
        "worktree_revision",
    ]
    context: dict[str, object] = {name: f"fixture-{name}" for name in text_fields}
    for name in [
        "accelerators_per_node",
        "capacity",
        "continuation_step",
        "framework_revert_round",
        "in_flight",
        "iteration",
        "limit",
        "max_issues_per_perf_eval",
        "nodes",
        "num_failed_attempts",
        "remaining",
        "retry",
        "round_number",
        "scheduled",
    ]:
        context[name] = 2
    # True renders the optional evaluation section of the core-policy prompts.
    context["agent_evaluation"] = True
    context["git_writable"] = True
    # True renders the Docker notes' reproduce-on-this-hardware sentence.
    context["hardware_attached"] = True
    for name in [
        "baseline_pending",
        "framework_benchmark_enabled",
        "framework_revert_applied",
        "gate_revalidation_pending",
        "has_history",
        "input_at_fault",
        "input_compared",
        "is_cold_start",
        "not_allowed",
        "official_eval_cadence_due",
        "official_evaluation_due",
        "profiling",
        "queue_budget_spent",
        "repair_seed",
        "require_unseen_id",
        "searched",
    ]:
        context[name] = False
    for name in [
        "failed_lessons",
        "gates",
        "inspirations",
        "notes",
        "prior_attempt_artifact_locations",
        "profile_fields",
        "ranked_bottlenecks",
        "read_only_paths",
        "recommended_skills",
        "results",
        "roadmap",
        "sections",
        "seeded_workspace_paths",
        "updated_hypothesis_ids",
    ]:
        context[name] = ()
    for name in [
        "campaign",
        "cap",
        "checkpoint",
        "ended_by",
        "input_failure",
        "input_first_attempt",
        "input_overdue",
        "invalid_type",
        "omitted",
        "portfolio_view",
    ]:
        context[name] = None
    issue = Issue(
        id=1,
        type=IssueType.FEATURE,
        title="Add streaming",
        description="Return token deltas.",
        created_by="fixture",
        created_iter=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    context.update(
        archive=ParetoArchiveView(axes=(), relative_noise=0.05, latest=None),
        decision=PreRoundDecision(need_profile=True, profile_focus="queue", reasoning="Measure."),
        evaluation_suspension=True,
        exhaustion_info=ExhaustionNotice(round_number=1, attempts=2, feedback="Retry."),
        facts=RunFacts(
            agent_git_writable=True, domain_id="generic", objective="Improve throughput."
        ),
        filenames={1: "issue-1.md"},
        groups=((IssueStatus.OPEN, (issue,)),),
        gate_approved_perf_metric=1.0,
        issue=issue,
        issue_id=1,
        issues=(issue,),
        issue_types=tuple(IssueType),
        ledger=FocusLedger(),
        messages=("Retry.",),
        modality="text_generation",
        note_sha256="0" * 64,
        objectives=(Objective(name="throughput", direction="max"),),
        observed_failure="accuracy failed",
        parent=Individual(id=1, generation=0),
        parent_base_accuracy=True,
        parent_round=1,
        pareto_archive_conflict=ArchiveConflict(
            dominators=(ArchiveDominator(round_number=1, metrics=()),)
        ),
        payload={"summary": "fixture"},
        plan=OrchestratorPlan.model_validate(
            {
                "task": "Batch requests.",
                "pass_criteria": "Correct.",
                "reasoning": "Reduce overhead.",
            }
        ),
        prior_review={"feedback": "Retry."},
        profile_execution="remote",
        provisional_candidates=1,
        rejected=(),
        regression_info=TerminalWorkspaceEdits(
            hypothesis_id="queue",
            outcome="falsified",
            round_number=1,
            parent_round=0,
            checkpoint=None,
        ),
        result=LocalValidationEvaluation(passed=True),
        sent_at_s=10.0,
        skills=(SkillFact(name="serving-systems", description="Tune queues."),),
        interface="service",
        workspace_sources=(),
        root_revision="revision",
        position=0,
        required_fields=tuple(ProfileField),
        missing_fields=tuple(ProfileField),
    )
    return context


def template_roots(path: Path, installed: Path) -> tuple[Path, tuple[Path, ...]]:
    """The root a packaged template renders from, and every root its includes may resolve in."""
    shared = installed / "vibesys" / "prompts" / "shared"
    common = shared.parent
    core_protocol = common / "core_protocol"
    parts = path.relative_to(installed).parts
    root = installed.joinpath(*parts[: parts.index("prompts") + 1])
    if path.is_relative_to(shared):
        root = shared
    elif path.is_relative_to(core_protocol):
        root = core_protocol
    return root, (root, shared, common, core_protocol)


def render_packaged_prompts(installed: Path) -> int:
    """Exercise the public renderer against every template in the unpacked wheel."""
    context = representative_context()
    workspace, resource_roots = stage_prompt_workspace(installed)
    source_objective = (
        "Improve throughput. Read "
        "`skills/domains/serving-systems/references/platforms/rocm/floor.md`."
    )
    context["objective"] = resolve_agent_resource_paths(
        source_objective, list(resource_roots.values())
    )
    context["facts"] = RunFacts(
        agent_git_writable=True, domain_id="generic", objective=str(context["objective"])
    )
    context["agent_gpu"] = {
        "max_gpus": 2,
        "max_time_minutes": 2,
        "read_only_paths": ("/data/models",),
        "tool_paths": (("ncu", "/usr/local/cuda/bin/ncu"),),
    }
    context["prompts"] = ("Review the candidate.",)
    context["steers"] = ({"mode": "steer", "text": "Focus on the cache."},)
    context["guidance"] = ("Keep the weights in bf16.",)
    context["text"] = "Switch the cache to fp8 now."
    # An operator's cancel_item notice, naming one attempt of each fate.
    context.update(
        action="cancel_item",
        subject="I-1",
        stopped=("A-1",),
        unqueued=("A-2",),
        parked=("A-3",),
        settling=("A-4",),
    )
    hidden = (
        build_project_path_policy(workspace, evaluator_source=None).resolve(workspace).hidden_paths
    )
    context.update(rich_path_context(workspace))
    runtime_paths = runtime_path_context(context)
    mounted_context, sandbox, mounts = container_context(workspace)
    assert_mounted_negative_controls(workspace, mounts, sandbox)
    assert_confinement_negative_controls(workspace)
    with pytest.raises(AssertionError, match="missing workspace input"):
        assert_resource_citations(
            source_objective,
            Path("source-objective-negative-control"),
            workspace,
            resource_roots,
            frozenset(),
        )
    stage_pruned_backend_workspace(installed)
    environment = Environment(autoescape=True)
    templates = sorted(
        path
        for path in installed.rglob("*")
        if path.suffix in {".j2", ".md"}
        and "prompts" in path.relative_to(installed).parts
        and path.name != "README.md"
    )
    assert templates
    # Every field any template reads from a reply variable, so the file renders: whether a
    # strategy's reply offers that field is the prompt tests' concern, not packaging's.
    referenced = {
        name
        for path in templates
        for name in re.findall(r"\b(?:implementer_)?reply\.(\w+)", path.read_text("utf-8"))
    }
    offered = reply_fields({"properties": {name: {} for name in referenced}})
    context["reply"] = context["implementer_reply"] = offered
    for path in templates:
        # Release resources may ship unrelated third-party Jinja files; only package
        # prompt trees form this renderer's contract.
        if "prompts" not in path.relative_to(installed).parts or path.name == "README.md":
            continue
        parts = path.relative_to(installed).parts
        root, search_roots = template_roots(path, installed)
        shared = installed / "vibesys" / "prompts" / "shared"
        common = shared.parent
        free, _ = resolve_free_variables(path, search_roots=search_roots)
        template_inputs = context
        if {"core_policy", "core_protocol", "serial_search"} & set(parts):
            template_inputs = context | core_policy_context()
        assert free <= template_inputs.keys() | {"response"}, (
            path,
            sorted(free - template_inputs.keys()),
        )
        values = template_context(path, template_inputs, mounted_context, free)
        rendered = render_template(path.relative_to(root).as_posix(), template_dir=root, **values)
        assert not re.search(r"{{|{%|{#", rendered), path
        assert_resource_citations(rendered, path, workspace, resource_roots, runtime_paths)
        assert_citations_survive_pruning(rendered, path, common)
        assert_mounted_citations(rendered, path, mounts, sandbox)
        for masked in hidden:
            assert str(masked.path) not in rendered, path
            assert masked.path.relative_to(workspace).as_posix() not in rendered, path
        assert_template_includes(path, search_roots, installed, environment)
    origins = {
        name: Path(filename).resolve()
        for name, module in sys.modules.items()
        if name.split(".")[0]
        in {
            "vibesys",
            "vs_prompts",
            "vs_runtime",
            "vs_issue_tracker",
            "vs_sandbox",
            "vs_project",
            "vs_core",
            "vs_agent",
            "vs_evaluation",
            "vs_slurm",
        }
        and (filename := getattr(module, "__file__", None)) is not None
        and filename
    }
    assert origins
    assert all(path.is_relative_to(installed) for path in origins.values()), origins
    return len(templates)


def core_policy_context() -> dict[str, object]:
    """Typed inputs of the dynamic core policy's templates (its own prompt context models)."""
    revision = RevisionRef(revision_id=RevisionId(root="a1"), digest="git-commit:a1")
    planner = PlannerPrompt(
        capacity=1,
        in_flight=0,
        remaining=3,
        base_revision=revision,
        base_accuracy_passed=True,
        main_revision=revision,
        offer_snapshot="fixture-offer",
        older_ids=(),
        baseline=(),
        input_failure=None,
        profiling=False,
    )
    evaluation = ReviewEvaluation(
        evidence_id=EvidenceId(root="e1"),
        kind=EvidenceKind.CORRECTNESS,
        revision=revision,
        status=ObservationStatus.SUCCEEDED,
        passed=False,
        feedback="fixture failure",
        feedback_cut=True,
    )
    return {
        "agent_git_writable": True,
        "domain_sections": dict.fromkeys(
            ("implementer", "judge", "single_agent", "orchestrator", "profiler"), ""
        ),
        "evaluations": (evaluation,),
        "planner": planner,
        "base_revision": revision,
        "parent_revision": revision,
        "worktree_revision": revision,
        "prior_revision": revision,
        "retained_revision": revision,
        "candidate": revision,
        "target": revision,
        "baseline": (),
        "buildable": (),
        "history": (),
        "evidence": (),
        # What `RenderArtifactsOwner` binds for each cited record, read from the evidence ledger.
        "cited_evidence": (
            {
                "evidence_id": "e1",
                "kind": "benchmark",
                "revision": revision,
                "status": "succeeded",
                "passed": True,
                "stage": "benchmark",
                "metrics": (
                    {"name": "throughput", "value": 3001.7, "direction": "max", "unit": "calls/s"},
                ),
                "partial": None,
                "feedback": "",
            },
        ),
        "offer_snapshot": "fixture-offer",
        "base_accuracy_passed": True,
        "repeated_failure": None,
        "comparisons": (),
        **core_protocol_variables(ComparisonPolicy()),
        "required_fields": (),
        "prior_failure_tail": "AssertionError: fixture",
        "prior_failure_fence": "```",
        "last_blocker": Blocker(
            kind=BlockerKind.FAILED,
            summary="tests/test_engine.py::test_decode fails to import",
            next_step="Make the import resolve first.",
        ),
        "rejection": "The candidate removes the retry loop.",
        # A correction names the request it repeats; the template only asks whether it is set.
        "of": "planning request",
        "turns_without_candidate": 2,
        # The outline turn of a plan-mode run cites the plan's documents (a staged input here).
        "documents": ("reference/reference.py",),
        # The handoff file of one planning call (`WriteHandoff`'s fields).
        "handoff_paths": (
            ".vibesys/state/agent-visible/run-1/orchestrator-handoff/calls/call-0001.md",
        ),
        "call": 1,
        "reasoning": "Batch the decode loop first.",
        "scheduled": (),
        "updates": (),
        "handoff": HandoffNote(
            expectations=(
                Expectation(
                    work_id="h1",
                    expected="Throughput rises.",
                    if_it_succeeds="Merge it.",
                    if_it_fails="Profile the queue.",
                ),
            ),
            doubts=("The benchmark may be noisy.",),
        ),
        # The run's main and its merges (`MergePrompt` and the planner's main fields).
        "main_revision": revision,
        "main_measurement": MainMeasurement(revision=revision, merge_id="m1", accuracy_passed=True),
        "merge_running": "m2",
        "mergeable": (MergeableRow(hypothesis_id="h1", revision=revision),),
        "merges": (
            MergeRecord(
                merge_id="m1",
                sequence=2,
                candidates=(MergeSource(hypothesis_id="h1", revision=revision),),
                base=revision,
                outcome=MergeOutcome.REFUSED,
                reason="The two changes rewrite the same kernel.",
            ),
        ),
        "rebase_onto": revision,
        "rebase_from": revision,
        "reply_rejected": (StrayUnapplied(index=1, hypothesis_id="h9"),),
        "combine": (
            CombineSource(hypothesis_id="h1", revision=revision, built_on=revision),
            CombineSource(hypothesis_id="h2", revision=revision, built_on=revision),
        ),
        "combine_missing": (CombinePick(hypothesis_id="h3", revision="c3"),),
        "merge_id": "m1",
        "candidates": (MergeSource(hypothesis_id="h1", revision=revision, built_on=revision),),
        "instructions": "Apply h1, then h2.",
        "watch_for": "Both change the scheduler.",
        # The issue queue's typed contexts.
        "board": (
            IssueBrief(id=1, type="feature", status="open", title="Add streaming", attempts=0),
        ),
        "analysis": "The health route is missing.",
        "max_new_issues": 2,
        "named": 3,
        "prior": (
            PriorPerformance(
                iteration=1,
                throughput_trend="improved",
                latency_trend="mixed",
                metrics_json="{}",
                new_issues=1,
            ),
        ),
        "files_touched": ("a.py",),
        "self_check": "Ran the tests.",
        # The multi strategy's prompt fields (`ImplementPrompt`, `JudgePrompt`, `ProfilerPrompt`).
        "framework": GateFacts(),
        "claims": ImplementerClaims(
            summary="fixture",
            expected_behavior="fixture",
            status="implemented",
            candidate_disposition="unassessed",
        ),
        "validation_failure": ValidationFailure(recipe="unit", error="boom", output="tail"),
        "validation_recipe_schema": '{"version": 1}',
        "profiler_kind": "linux_cpu",
        # The single strategy's plan prompt (`PlanPrompt`) fields.
        "benchmark_enabled": True,
        "exhaustion_feedback": "fixture feedback",
        "exhaustion_round": 1,
        "frontier": (),
        "last_profile": None,
        "max_rounds": 3,
        "input_value": None,
        "measurement_noise": None,
        "passed": (),
        "older_rounds": 0,
        "pending_claims": (),
        "regression_kind": None,
        "regression_round": None,
        # The multi router's `PreRoundPrompt`: the round whose idea made the base.
        "base_round": 2,
        "base_value": 173.25,
        # `RoundPrompt` fields.
        "activation_evidence": "fixture",
        "dominated_by_rounds": (),
        "falsification_criteria": "fixture",
        "invariants": "fixture",
        "minimum_ratio": 1.0,
        "no_valid_reply": None,
        "problem": None,
        "agent": "implementer",
        "retained": revision,
        "errors": (ReplyError(path=("reasoning",), code=ReplyErrorCode.MISSING),),
        "profiler_id": "linux_cpu",
        "title": "fixture title",
    }


def template_context(
    path: Path,
    context: dict[str, object],
    mounted: dict[str, object],
    free: frozenset[str],
) -> dict[str, object]:
    """Project each typed template family's representative inputs."""
    values = context.copy()
    if path.parent.name in {"docker", "modal", "skypilot"}:
        values.update(mounted)
    if path.parent.name == "_progress" and path.name == "profile.j2":
        values["summary"] = ProfilerSummary(
            analysis="Measured queue.", bottlenecks="Queue.", suggestions="Batch."
        )
    if path.name == "portfolio.j2":
        # The portfolio template reads `evaluations` as the evaluation queue; the review
        # templates read the same name as a candidate's evaluations.
        values["evaluations"] = EvaluationQueueView(
            pick="ranked", slots=1, input_holds_slot=True, ranking=("w1",)
        )
    if "response" in free:
        values["response"] = response_for(path)
    if path.parent.name == "profilers" and path.stem in {
        kind.value for kind in ACTIVE_PROFILER_KINDS
    }:
        definition = profiler_definition(ProfilerKind(path.stem))
        values.update(
            profiler_support_name=definition.support_name, profiler_mcp_name=definition.mcp_name
        )
    return values


def assert_template_includes(
    path: Path, roots: tuple[Path, ...], installed: Path, environment: Environment
) -> None:
    """Static imports/includes resolve within the same packaged loader search roots."""
    for reference in meta.find_referenced_templates(environment.parse(path.read_text())):
        if reference is None:
            # Dynamic modality targets render in the corpus; each fragment renders too.
            continue
        resolved = next(
            ((root / reference).resolve() for root in roots if (root / reference).is_file()), None
        )
        assert resolved is not None, (path, reference)
        assert resolved.is_relative_to(installed), (path, reference)


def response_for(path: Path) -> object:
    """Each progress template receives its real response type."""
    if path.name == "judge.j2":
        return JudgeResponse.model_validate(
            {"verdict": "pass", "analysis": "Correct.", "feedback": ""}
        )
    if path.name == "single_response.j2":
        return SingleAgentRoundResponse.model_validate(
            {
                "summary": "Implemented.",
                "expected_behavior": "Correct.",
                "self_review": "Checked.",
                "feedback": "",
                "verdict": "pass",
                "bottlenecks": "queue",
                "suggestions": "batch",
                "profile_analysis": "Measured.",
            }
        )
    return ImplementerResponse.model_validate(
        {
            "summary": "Implemented.",
            "expected_behavior": "Correct.",
            "status": "continue",
        }
    )


def stage_prompt_workspace(
    installed: Path,
    backend: ComputeBackend | None = None,
) -> tuple[Path, dict[str, Path]]:
    """Use the same workspace staging plan and confinement policy as real runs."""
    suffix = f"-{backend.value}" if backend is not None else ""
    workspace = installed.parent / f"agent-workspace{suffix}"
    input_dir = installed.parent / f"input{suffix}"
    input_dir.mkdir()
    (input_dir / "OBJECTIVE.md").write_text("Improve throughput.")
    (input_dir / "agent.toml").write_text("# private agent settings")
    skill_sources = discover_skill_dirs(installed / "vibesys" / "_skills")
    materializer = ProjectMaterializer(
        workspace,
        effects=FakeProjectMaterializationEffects(),
        log=lambda _: None,
        sdk_roots=SDKRoots(
            checkout=installed / "vibesys" / "_sdk", packaged=installed / "vibesys" / "_sdk"
        ),
        excluded_dirs=EXCLUDED_WORKSPACE_DIRS,
        # This module also runs in an isolated interpreter over the built wheel,
        # where the tests package is not importable.
        git=Git(OsProcesses()),
    )
    materializer.create()
    resource_roots = {source.name: source for source in skill_sources}
    profiler_sources = {}
    for kind in sorted(ACTIVE_PROFILER_KINDS):
        definition = profiler_definition(kind)
        profiler_sources[definition.support_name] = (
            installed / "vibesys" / "_resources" / "profilers" / kind.value
        )
        profiler_sources.update(
            {name: Path(source) for source, name in profiler_support_extra(definition)}
        )
    resource_roots.update(profiler_sources)
    primary_name, primary_source = next(iter(profiler_sources.items()))
    plan = build_workspace_materialization_plan(
        workspace,
        existing=False,
        input_dir=input_dir,
        evaluator_source=None,
        skill_sources=list(skill_sources),
        input_project_dir=None,
        profiler_support_path=str(primary_source),
        profiler_support_name=primary_name,
        skill_excluded_relative_paths=platform_skill_excluded_paths(backend),
        profiler_support_extra=tuple(
            (str(source), name) for name, source in profiler_sources.items()
        ),
    )
    materializer.materialize(plan, existing=False)
    # Agent drivers install discovery copies before each turn. Execute the same
    # product copy specification, including platform exclusions, after staging.
    for source in skill_sources:
        materializer.copy_tree(
            skill_copy(
                source,
                workspace / ".agents" / "skills" / source.name,
                platform_skill_excluded_paths(backend),
            )
        )
    secret = Project.open(workspace).state.log_directory("contract")
    secret.mkdir(parents=True, exist_ok=True)
    (secret / "effective-objective.md").write_text("Hidden authoritative objective.")
    return workspace, resource_roots


def rich_path_context(workspace: Path) -> dict[str, object]:
    """Use real artifact writers and typed collections for cited role inputs."""
    reference = workspace / "reference"
    reference.mkdir()
    (workspace / "progress.md").write_text("Issue progress ledger.")
    for filename in ("model.py", "meta.json", "config.json", "reference.py"):
        (reference / filename).write_text("reference fixture")
    artifact = workspace / "artifacts" / "evaluation.json"
    artifact.parent.mkdir()
    artifact.write_text('{"status":"failed"}')
    sessions = workspace / "sessions"
    sessions.mkdir()
    (sessions / "conversation.jsonl").write_text("{}\n")
    return {
        "read_only_paths": ("reference",),
        "gates": ("accuracy", "benchmark"),
        "seeded_workspace_paths": ("reference/model.py",),
        "objective_location": "OBJECTIVE.md",
        "reference_path": "reference",
        "session_state_dir": "sessions",
        "profiling": True,
    }


def assert_resource_citations(
    rendered: str,
    template: Path,
    workspace: Path,
    resource_roots: dict[str, Path],
    runtime_paths: frozenset[str],
) -> None:
    """Check packaged skill/profiler inputs and explicit runtime file receipts."""
    names = "|".join(re.escape(name) for name in resource_roots)
    for citation in re.findall(
        rf"(?<![\w./-])(?:skills/domains/|\.agents/skills/)?(?:{names})/[\w./-]+",
        rendered,
    ):
        relative = Path(citation.rstrip("."))
        resource = relative
        for prefix in (Path("skills/domains"), Path(".agents/skills")):
            if resource.is_relative_to(prefix):
                resource = resource.relative_to(prefix)
                break
        packaged = resource_roots[resource.parts[0]].joinpath(*resource.parts[1:])
        assert packaged.exists(), (template, citation, "missing packaged resource")
        assert_workspace_path(relative.as_posix(), template, workspace)
    roots = {Path(reference).parts[0] for reference in runtime_paths}
    root_names = "|".join(re.escape(root) for root in roots)
    cited_text = "\n".join(
        (*re.findall(r"`([^`]+)`", rendered), *re.findall(r'"([^"\n]+)"', rendered))
    )
    citations = set(re.findall(rf"(?<![\w.-])(?:{root_names})/[\w./-]+", cited_text))
    citations.update(
        citation for citation in re.findall(r"`([^`]+)`", rendered) if citation in roots
    )
    for reference in citations:
        if "..." not in reference:
            assert_workspace_path(reference.rstrip("."), template, workspace)


def assert_citations_survive_pruning(rendered: str, template: Path, common: Path) -> None:
    """A backend's template must not cite a ``serving-systems`` path its workspace prunes."""
    if not template.is_relative_to(common / "backend"):
        return
    backend = ComputeBackend(template.relative_to(common / "backend").parts[0])
    excluded = tuple(Path(item) for item in platform_skill_excluded_paths(backend))
    for citation in re.findall(
        r"(?:skills/domains/|\.agents/skills/)?serving-systems/[\w./-]+", rendered
    ):
        inside = Path(citation.rstrip(".")).parts
        inside = inside[inside.index("serving-systems") + 1 :]
        assert not any(Path(*inside).is_relative_to(item) for item in excluded), (
            template,
            citation,
            "missing workspace input",
        )


def runtime_path_context(context: dict[str, object]) -> frozenset[str]:
    """Collect known existing paths from actual artifact writers and typed receipts."""
    names = ("objective_location", "reference_path", "session_state_dir")
    return frozenset(str(context[name]) for name in names) | {
        "reference/meta.json",
        "reference/config.json",
        "reference/reference.py",
        "progress.md",
    }


def container_context(
    workspace: Path,
) -> tuple[dict[str, object], DockerSandbox, tuple[HostResource, ...]]:
    """Prove environment pointers map to explicit readable sandbox grants."""
    facts = ModalEnvironmentFacts(
        gpu="fixture-gpu", app_name="fixture-app", reference_path="reference"
    )
    runtime = workspace.parent / "runtime.md"
    runtime.write_text("Runtime environment contract.")
    history = workspace.parent / "history"
    history.mkdir()
    (history / "prior.md").write_text("Prior measured candidate.")
    resources = (
        host_resource_for_mount(runtime, facts.runtime_container_path, read_only=True),
        host_resource_for_mount(history, "/opt/vibesys-history", read_only=True),
    )
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image="fixture",
        resources=resources,
        docker=ScriptedDockerCli(),
        threads=OsThreads(),
    )
    assert sandbox.agent_path(runtime) == facts.runtime_container_path
    assert sandbox.agent_path(history / "prior.md") == "/opt/vibesys-history/prior.md"
    context = {
        "runtime_container_path": facts.runtime_container_path,
        "history_root": "/opt/vibesys-history",
        "gpu": facts.gpu,
        "app_name": facts.app_name,
        "reference_path": facts.reference_path,
    }
    return context, sandbox, resources


def assert_mounted_citations(
    rendered: str,
    template: Path,
    mounts: tuple[HostResource, ...],
    sandbox: DockerSandbox,
) -> None:
    """Every emitted runtime/history pointer resolves through a declared read-only mount."""
    for citation in re.findall(r"/opt/vibesys[\w./-]*", rendered):
        agent_path = Path(citation.rstrip("."))
        matches = [
            resource
            for resource in mounts
            if agent_path.is_relative_to(Path(resource.agent_path or resource.path))
        ]
        assert matches, (template, citation, "undeclared container path")
        resource = max(matches, key=lambda item: len(Path(item.agent_path or item.path).parts))
        assert resource.access is HostResourceAccess.READ_ONLY
        relative = agent_path.relative_to(Path(resource.agent_path or resource.path))
        host_path = resource.path / relative if relative != Path() else resource.path
        assert host_path.exists(), (template, citation, "missing mounted input")
        grant = resource.path.resolve(strict=True)
        resolved_host = host_path.resolve(strict=True)
        assert resolved_host.is_relative_to(grant), (template, citation, "outside mount grant")
        assert sandbox.agent_path(host_path) == str(agent_path), (template, citation)


def assert_workspace_path(citation: str, template: Path, workspace: Path) -> None:
    """Resolve a cited input using the real confinement policy, including symlinks."""
    candidate = workspace / citation
    assert candidate.exists(), (template, citation, "missing workspace input")
    resolved = candidate.resolve(strict=True)
    assert resolved.is_relative_to(workspace), (template, citation, "outside workspace")
    hidden = (
        build_project_path_policy(workspace, evaluator_source=None).resolve(workspace).hidden_paths
    )
    assert not any(resolved.is_relative_to(mask.path) for mask in hidden), (
        template,
        citation,
        "hidden path",
    )


def assert_confinement_negative_controls(workspace: Path) -> None:
    """Prove relative hidden paths, traversal and symlinks cannot pass the checker."""
    hidden = (
        build_project_path_policy(workspace, evaluator_source=None)
        .resolve(workspace)
        .hidden_paths[0]
        .path
    )
    outside = workspace.parent / "outside.md"
    outside.write_text("outside the agent workspace")
    link = workspace / "escape.md"
    link.symlink_to(outside)
    for reference, diagnostic in (
        (hidden.relative_to(workspace).as_posix(), "hidden path"),
        ("../outside.md", "outside workspace"),
        ("escape.md", "outside workspace"),
    ):
        with pytest.raises(AssertionError, match=diagnostic):
            assert_workspace_path(reference, Path("negative-control"), workspace)


def stage_pruned_backend_workspace(installed: Path) -> tuple[ComputeBackend, Path]:
    """Stage one backend's workspace with the real pruning, as proof the exclusions apply.

    Staging copies the whole skill tree, which dominates this contract's cost, so only
    one backend is staged for real. Every backend's exclusions come from the same product
    function, and ``assert_resource_citations`` applies them to the citations of the
    backend's own templates against the shared workspace.
    """
    backend = next(item for item in ComputeBackend if platform_skill_excluded_paths(item))
    workspace, _ = stage_prompt_workspace(installed, backend)
    platforms = workspace / "serving-systems" / Path(*PLATFORMS_PARENT)
    for filename in PLATFORM_SKELETON:
        assert (platforms / backend.value / filename).is_file(), (backend, filename)
    for excluded in platform_skill_excluded_paths(backend):
        assert not (workspace / "serving-systems" / excluded).exists(), (backend, excluded)
    return backend, workspace


def assert_mounted_negative_controls(
    workspace: Path,
    mounts: tuple[HostResource, ...],
    sandbox: DockerSandbox,
) -> None:
    """A container mount grants its tree, not symlink targets or traversal siblings."""
    outside = workspace.parent / "ungranted.md"
    outside.write_text("Not granted to the agent.")
    (workspace.parent / "history" / "escape.md").symlink_to(outside)
    for citation in ("/opt/vibesys-history/escape.md", "/opt/vibesys-history/../ungranted.md"):
        with pytest.raises(AssertionError, match="outside mount grant"):
            assert_mounted_citations(citation, Path("negative-control"), mounts, sandbox)
