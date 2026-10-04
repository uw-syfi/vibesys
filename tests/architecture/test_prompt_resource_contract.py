"""Render wheel-owned prompts, without an editable checkout as a resource fallback."""

from __future__ import annotations

import re
import shutil
import sys
import sysconfig
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from jinja2 import Environment, meta

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
from vibesys.orchestration.dynamic.models import PortfolioView, SteerNote
from vibesys.orchestration.dynamic.prompts import (
    EvaluationLine,
    EvaluationResumeLine,
    FailureTail,
    RepeatedFailureLine,
)
from vibesys.orchestration.evolve.population import Individual
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
    ProfilerCampaign,
)
from vibesys.orchestration.multi.files import MultiFiles
from vibesys.orchestration.profilers import (
    ACTIVE_PROFILER_KINDS,
    ProfilerSummary,
    profiler_definition,
    profiler_support_extra,
)
from vibesys.orchestration.progress import ProgressLog
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.skill_selection import (
    PLATFORM_SKELETON,
    PLATFORMS_PARENT,
    platform_skill_excluded_paths,
    resolve_agent_resource_paths,
)
from vibesys.profile_focus import FocusLedger
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.run.contracts import ProfilerKind
from vibesys.run.project_policy import build_project_path_policy
from vibesys.run.workspace_policy import (
    EXCLUDED_WORKSPACE_DIRS,
    build_workspace_materialization_plan,
    skill_copy,
)
from vs_evaluation.api import EvaluationOperationSnapshot, EvaluationState, ProfileField
from vs_issue_tracker.api import Issue, IssueStatus, IssueType
from vs_project.api import Project
from vs_prompts.api import resolve_free_variables
from vs_runtime.api import LocalValidationEvaluation, ResolvedSkillResources, RunFacts, SkillFact
from vs_runtime.api.infrastructure import (
    ModalEnvironmentFacts,
    ProjectMaterializer,
    SDKRoots,
    discover_skill_dirs,
)
from vs_runtime.api.testing import FakeProjectMaterializationEffects
from vs_sandbox.api import DockerSandbox, HostResourceAccess, host_resource_for_mount

if TYPE_CHECKING:
    from vs_sandbox.api import HostResource

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def packaged_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build in private scratch so concurrent packaging checks share no build output."""
    scratch = tmp_path_factory.mktemp("prompt-wheel")
    checkout = scratch / "checkout"
    shutil.copytree(
        ROOT,
        checkout,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache"
        ),
    )
    run_test_command(
        ["uv", "build", "--wheel", "--out-dir", str(scratch / "dist")],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    installed = scratch / "installed"
    with zipfile.ZipFile(next((scratch / "dist").glob("vibesys-*.whl"))) as archive:
        archive.extractall(installed)
    return installed


def assert_every_source_prompt_is_in_the_wheel(packaged_tree: Path) -> None:
    """A forgotten package-data glob must fail even when editable installs render it."""
    sources = [ROOT / "src", *(ROOT / "libs").glob("*/src")]
    expected = {
        path.relative_to(source)
        for source in sources
        for path in source.rglob("*")
        if path.is_file() and path.suffix in {".j2", ".md"} and "prompts" in path.parts
    }
    assert expected
    missing = sorted(str(path) for path in expected if not (packaged_tree / path).is_file())
    assert not missing, f"Prompts missing from wheel: {missing}"


def test_every_packaged_prompt_renders_without_checkout_fallback(packaged_tree: Path) -> None:
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
            str(packaged_tree),
            sysconfig.get_path("purelib"),
            str(Path(__file__)),
        ],
        cwd=packaged_tree,
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
        "history",
        "history_root",
        "hypothesis",
        "hypothesis_id",
        "implementer_artifact_location",
        "input_partial",
        "interrupted_revision",
        "load_levels_json",
        "objective",
        "objective_location",
        "official_evaluation_reason",
        "older_ids",
        "outcome",
        "parent_revision",
        "pareto_archive_location",
        "pass_criteria",
        "plan_artifact_location",
        "plateau_warning",
        "prior_attempt",
        "prior_records_json",
        "prior_revision",
        "profile_focus",
        "profile_name",
        "profiler_entry",
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
        "session_state_dir",
        "signature",
        "stage_failure",
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
        "official_eval_every",
        "remaining",
        "retry",
        "round_number",
        "scheduled",
    ]:
        context[name] = 2
    for name in [
        "framework_benchmark_enabled",
        "framework_revert_applied",
        "gate_revalidation_pending",
        "has_history",
        "is_cold_start",
        "not_allowed",
        "official_eval_cadence_due",
        "official_evaluation_due",
        "profiling",
        "repair_seed",
        "require_unseen_id",
        "searched",
    ]:
        context[name] = False
    for name in [
        "failed_lessons",
        "inspirations",
        "notes",
        "prior_attempt_artifact_locations",
        "ranked_bottlenecks",
        "read_only_paths",
        "recommended_skills",
        "results",
        "sections",
        "seeded_workspace_paths",
        "updated_hypothesis_ids",
    ]:
        context[name] = ()
    for name in [
        "campaign",
        "cap",
        "checkpoint",
        "input_failure",
        "invalid_type",
        "omitted",
        "portfolio_view",
        "timed_out",
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
        capture=EvaluationOperationSnapshot(
            handle_id="profile-capture",
            candidate_revision="fixture-candidate",
            state=EvaluationState.SUCCEEDED,
            evidence_recorded=True,
            evidence_ids=("a" * 64,),
        ),
        decision=PreRoundDecision(need_profile=True, profile_focus="queue", reasoning="Measure."),
        evaluations=(
            EvaluationLine(
                "revision", ("accuracy",), "failed", FailureTail("failure", truncated=False)
            ),
        ),
        evaluation_suspension=True,
        exhaustion_info=ExhaustionNotice(round_number=1, attempts=2, feedback="Retry."),
        facts=RunFacts(domain_id="generic", objective="Improve throughput."),
        failure=FailureTail("failed at engine.py:1", truncated=True),
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
        repeated=RepeatedFailureLine(
            kind="measurement",
            stage="benchmark",
            signature="measurement failed",
            count=2,
            instruction="Inspect the measurement failure before retrying.",
        ),
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
        portfolio_view=PortfolioView(),
    )
    return context


def render_packaged_prompts(installed: Path) -> int:
    """Exercise the public renderer against every template in the unpacked wheel."""
    context = representative_context()
    workspace, resource_roots = stage_prompt_workspace(installed)
    source_objective = (
        "Improve throughput. Read "
        "`resources/skills/serving-systems/references/platforms/rocm/floor.md`."
    )
    context["objective"] = resolve_agent_resource_paths(
        source_objective, list(resource_roots.values())
    )
    context["facts"] = RunFacts(domain_id="generic", objective=str(context["objective"]))
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
    selected_workspaces = stage_backend_workspaces(installed)
    environment = Environment(autoescape=True)
    templates = sorted(
        path
        for path in installed.rglob("*")
        if path.suffix in {".j2", ".md"}
        and "prompts" in path.relative_to(installed).parts
        and path.name != "README.md"
    )
    assert templates
    for path in templates:
        # Release resources may ship unrelated third-party Jinja files; only package
        # prompt trees form this renderer's contract.
        if "prompts" not in path.relative_to(installed).parts or path.name == "README.md":
            continue
        shared = installed / "vibesys" / "prompts" / "shared"
        common = shared.parent
        parts = path.relative_to(installed).parts
        root = installed.joinpath(*parts[: parts.index("prompts") + 1])
        if path.is_relative_to(shared):
            root = shared
        search_roots = (root, shared, common)
        free, _ = resolve_free_variables(path, search_roots=search_roots)
        assert free <= context.keys() | {"response"}, (path, sorted(free - context.keys()))
        values = template_context(path, context, mounted_context, free)
        rendered = render_template(path.relative_to(root).as_posix(), template_dir=root, **values)
        assert not re.search(r"{{|{%|{#", rendered), path
        citation_workspace, citation_resources = workspace, resource_roots
        if path.is_relative_to(common / "backend"):
            backend = ComputeBackend(path.relative_to(common / "backend").parts[0])
            citation_workspace, citation_resources = selected_workspaces[backend]
        assert_resource_citations(
            rendered, path, citation_workspace, citation_resources, runtime_paths
        )
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
    if "response" in free:
        values["response"] = response_for(path)
    if path.name == "profiler_resume_prompt.j2":
        values["results"] = (
            EvaluationOperationSnapshot(
                handle_id="evaluation-1",
                state=EvaluationState.FAILED,
                evidence_recorded=False,
                failure="Failed accuracy.",
            ).model_dump(mode="json"),
        )
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
            "hypothesis_outcome": "continue",
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
    skill_sources = discover_skill_dirs(installed / "vibesys" / "_resources" / "skills")
    materializer = ProjectMaterializer(
        workspace,
        effects=FakeProjectMaterializationEffects(),
        log=lambda _: None,
        sdk_roots=SDKRoots(
            checkout=installed / "vibesys" / "_sdk", packaged=installed / "vibesys" / "_sdk"
        ),
        excluded_dirs=EXCLUDED_WORKSPACE_DIRS,
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
    files = MultiFiles.open(workspace)
    archive = ParetoArchiveView(axes=(), relative_noise=0.05, latest=None)
    files.write_pareto(archive)
    plan = OrchestratorPlan.model_validate(
        {
            "task": "Batch requests.",
            "pass_criteria": "Correct.",
            "reasoning": "Reduce overhead.",
        }
    )
    plan_location = files.write_plan(1, plan)
    implementation_location = files.write_implementer(
        1,
        1,
        ImplementerResponse.model_validate(
            {
                "summary": "Implemented.",
                "expected_behavior": "Correct.",
            }
        ),
    )
    entry = ProgressLog(workspace, files.progress).append(
        1,
        render_template(
            "_progress/profile.j2",
            template_dir=PROMPTS_DIR / "shared",
            round_number=1,
            summary=ProfilerSummary(
                analysis="Measured queue.", bottlenecks="Queue.", suggestions="Batch."
            ),
        ),
    )
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
        "plan": plan,
        "plan_artifact_location": plan_location,
        "implementer_artifact_location": implementation_location,
        "prior_attempt_artifact_locations": (implementation_location,),
        "roadmap_location": files.roadmap_location,
        "progress_location": files.progress_location,
        "pareto_archive_location": files.pareto_location,
        "validation_location": files.validation_location,
        "validation_recipe_contract_location": files.validation_schema_location,
        "current_round_location": entry.location,
        "regression_entry": entry,
        "exhaustion_entry": entry,
        "profiler_entry": entry,
        "campaign": ProfilerCampaign(
            progress_location=files.progress_location, evidence_location=files.profiler_location(1)
        ),
        "read_only_paths": ("reference",),
        "seeded_workspace_paths": ("reference/model.py",),
        "history_root": files.progress_location,
        "notes": (
            SteerNote(
                note_sha256="0" * 64,
                text="Inspect artifacts/evaluation.json.",
                sent_at_s=1.0,
                interrupt=False,
            ),
        ),
        "results": (
            EvaluationResumeLine(
                handle_id="evaluation-1",
                status="failed",
                candidate_revision="candidate",
                evaluator_revision="evaluator",
                evidence_ids=("evidence-1",),
                artifact_refs=("artifacts/evaluation.json",),
                detail="Failed accuracy.",
            ),
        ),
        "recommended_skills": (
            ResolvedSkillResources(
                name="serving-systems",
                router_path="serving-systems/SKILL.md",
                resource_paths=("serving-systems/references/tooling/openai-api.md",),
                purpose="Follow the API contract.",
            ),
        ),
        "objective_location": "OBJECTIVE.md",
        "reference_path": "reference",
        "gate_approved_evaluation_artifact": "artifacts/evaluation.json",
        "session_state_dir": "sessions",
        "profiling": True,
        "framework_revert_applied": True,
        "gate_revalidation_pending": True,
        "official_evaluation_due": True,
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
        rf"(?<![\w./-])(?:resources/skills/|\.agents/skills/)?(?:{names})/[\w./-]+",
        rendered,
    ):
        relative = Path(citation.rstrip("."))
        resource = relative
        for prefix in (Path("resources/skills"), Path(".agents/skills")):
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


def runtime_path_context(context: dict[str, object]) -> frozenset[str]:
    """Collect known existing paths from actual artifact writers and typed receipts."""
    names = (
        "objective_location",
        "reference_path",
        "progress_location",
        "roadmap_location",
        "pareto_archive_location",
        "plan_artifact_location",
        "implementer_artifact_location",
        "current_round_location",
        "validation_location",
        "validation_recipe_contract_location",
        "gate_approved_evaluation_artifact",
        "session_state_dir",
    )
    campaign = context["campaign"]
    assert isinstance(campaign, ProfilerCampaign)
    return frozenset(str(context[name]) for name in names) | {
        "reference/meta.json",
        "reference/config.json",
        "reference/reference.py",
        "progress.md",
        campaign.evidence_location,
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
    sandbox = DockerSandbox(host_workspace=str(workspace), image="fixture", resources=resources)
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


def stage_backend_workspaces(installed: Path) -> dict[ComputeBackend, tuple[Path, dict[str, Path]]]:
    """Verify citations after the same foreign-platform pruning a real run applies."""
    selected = {}
    for backend in ComputeBackend:
        workspace, resources = stage_prompt_workspace(installed, backend)
        platforms = workspace / "serving-systems" / Path(*PLATFORMS_PARENT)
        for filename in PLATFORM_SKELETON:
            assert (platforms / backend.value / filename).is_file(), (backend, filename)
        for excluded in platform_skill_excluded_paths(backend):
            assert not (workspace / "serving-systems" / excluded).exists(), (backend, excluded)
        selected[backend] = (workspace, resources)
    return selected


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
