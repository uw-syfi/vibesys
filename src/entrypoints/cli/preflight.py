"""``vibesys preflight [run arguments]``: the checks only a run's own code can make.

The host helper's ``run.preflight`` runs this with exactly the arguments ``run.start``
would launch with, in the project, under the same interpreter, and reads the one
``PreflightReport`` line it prints. The configuration check is the run's own
(``validated_run_request``), so a run whose configuration passes here does not fail
its configuration validation at launch. Nor on its compute backend's accelerator: when
the run's environment would hand the agent an accelerator this host cannot provide, the
settings fail with the reason the run itself would stop on.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from entrypoints.cli.config import require_repository_owner
from entrypoints.cli.loops import validated_run_request
from entrypoints.cli.preflight_cause import settings_cause, wire_cause
from launch import orchestration_roles
from server.host_preflight import (
    Backend,
    BackendUnavailable,
    Check,
    CheckId,
    CheckState,
    DeclareBuild,
    PreflightReport,
    SettingsInvalid,
    SettingsValid,
    SysboxMissing,
    backend_unavailable_text,
    clip,
    one_line,
)
from vibesys.api import ConfigurationError, TaskImageUnsupported, cli_wording
from vibesys.api.request import (
    TaskImageError,
    run_environment_uses_docker,
    run_host_shortfall,
)
from vs_agent.api.images import AGENT_IMAGE_REPOSITORY
from vs_project.api import (
    Git,
    GitError,
    GitTracker,
    NullGitTrackerEvents,
    open_git_repository,
)
from vs_sandbox.api.command_translation import PROJECT_ROOT_TOKEN
from vs_sim.api import OsProcesses, SubprocessProbe

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.api import RunRequest
    from vibesys.api.request import HostShortfall, InputBundle
    from vs_github.api import GitHubCLI
    from vs_project.api import GitRepository
    from vs_sim.api import CommandProbe

DOCKER_TIMEOUT_SECONDS = 5.0
AGENT_IMAGES_COMMAND = ("docker", "image", "ls", "--quiet", AGENT_IMAGE_REPOSITORY)
"""Lists the agent images built on this machine; Docker reuses their layers in a new build."""
ACCELERATOR_UNAVAILABLE = "accelerator_unavailable"
"""The settings code when this host lacks what the run checks before its image build.

That is the compute backend's accelerator, or the Sysbox runtime a docker-in-docker task needs.
"""


def run_preflight(argv: list[str], parse: Callable[[list[str]], Namespace]) -> None:
    """Print the report for a run started with ``argv``; ``parse`` parses as a run does."""
    report = preflight_report(argv, parse, SubprocessProbe())
    sys.stdout.write(report.model_dump_json() + "\n")


def preflight_report(
    argv: list[str],
    parse: Callable[[list[str]], Namespace],
    probe: CommandProbe,
    repository: Callable[[Path], GitRepository] | None = None,
    github: GitHubCLI | None = None,
) -> PreflightReport:
    """Validate ``argv`` as a run would, then check the agent image and the evaluator.

    ``repository`` opens the project's repository (real Git by default) for the check that a
    new run's baseline is clean, and ``github`` is the client (real ``gh`` by default) that
    says whether a run that publishes has a GitHub account to publish from.
    """
    try:
        args = parse(argv)
        request, config = validated_run_request(args, probe)
        require_repository_owner(args, config, github)
        roles = orchestration_roles(request.orchestration)
    except TaskImageError as error:
        # Refused while the arguments were parsed, before any task is known to the caller.
        return _task_image_refusal(error, _task_named_in(argv))
    except ConfigurationError as error:
        diagnostic = error.diagnostic
        return _invalid_settings(
            SettingsInvalid(
                code=diagnostic.code,
                stage=diagnostic.stage,
                message=diagnostic.message,
                cause=settings_cause(diagnostic),
            )
        )
    try:
        shortfall = run_host_shortfall(request, probe)
    except TaskImageError as error:
        return _task_image_refusal(error, args.input_bundle.task_name)
    changes = () if args.resume is not None else project_changes(request.project_root, repository)
    report = _resolved_report(args, request, shortfall, probe, roles)
    return report.model_copy(update={"project_changes": changes})


def _task_named_in(argv: list[str]) -> str | None:
    """The task ``--task`` names in ``argv``, if it names one."""
    for index, word in enumerate(argv):
        if word == "--task" and index + 1 < len(argv):
            return argv[index + 1]
        if word.startswith("--task="):
            return word.removeprefix("--task=")
    return None


def _task_image_refusal(error: TaskImageError, task: str | None) -> PreflightReport:
    """The run refuses the task's Dockerfile before it builds anything; report it as settings."""
    refusal = TaskImageUnsupported(task=task, problem=str(error))
    return _invalid_settings(
        SettingsInvalid(
            code=refusal.code,
            stage=refusal.stage,
            message=cli_wording(refusal),
            cause=wire_cause(refusal),
        )
    )


PREFLIGHT_RUN_ID = "preflight"
"""The run id the clean-baseline check reads the project's repository under; it starts no run."""


def project_changes(
    root: Path, repository: Callable[[Path], GitRepository] | None = None
) -> tuple[str, ...]:
    """The changes in the project's own repository that stop a new run at launch, sorted.

    The run's own tracker decides (``GitTracker.baseline_changes``, the check
    ``existing project repository must be clean`` makes), so a project this reports clean does
    not fail that check. A project that is not a repository yet, or whose repository cannot be
    read, reports none: the run creates the repository, and other checks name the rest.
    """
    if not (root / ".git").exists():
        return ()
    events = NullGitTrackerEvents()
    opened = (
        open_git_repository(root, git=Git(OsProcesses()), faults=events)
        if repository is None
        else repository(root)
    )
    tracker = GitTracker(root, events=events, run_id=PREFLIGHT_RUN_ID, repository=opened)
    try:
        return tuple(tracker.baseline_changes())
    except (GitError, ValueError):
        return ()


def _resolved_report(
    args: Namespace,
    request: RunRequest,
    shortfall: HostShortfall | None,
    probe: CommandProbe,
    roles: tuple[str, ...],
) -> PreflightReport:
    """The report for settings that resolved: the host's shortfall, else the image and evaluator."""
    backend = Backend(request.backend.value)
    if shortfall is not None:
        # The run stops on this before its image build, seconds after it starts.
        return PreflightReport(
            settings=_shortfall_settings(shortfall, args.input_bundle.task_name, request),
            uses_docker=run_environment_uses_docker(request.run_environment),
            agent_image=_not_checked(CheckId.AGENT_IMAGE),
            evaluator=_not_checked(CheckId.EVALUATOR_AVAILABLE),
            backend=backend,
            roles=roles,
        )
    return valid_report(
        uses_docker=run_environment_uses_docker(request.run_environment),
        bundle=args.input_bundle,
        probe=probe,
        roles=roles,
        backend=backend,
    )


def _shortfall_settings(
    shortfall: HostShortfall, task: str, request: RunRequest
) -> SettingsInvalid:
    """The settings failure for what the host lacks: typed when it is the task's backend."""
    if shortfall.backend is None:
        return SettingsInvalid(
            code=ACCELERATOR_UNAVAILABLE,
            stage="compute_backend",
            message=shortfall.reason,
            cause=SysboxMissing(task=clip(task), problem=clip(shortfall.reason)),
        )
    cause = BackendUnavailable(
        task=task,
        backend=Backend(shortfall.backend.value),
        declared=tuple(Backend(each.value) for each in request.input_bundle.declared_backends),
    )
    return SettingsInvalid(
        code=ACCELERATOR_UNAVAILABLE,
        stage="compute_backend",
        message=cli_backend_wording(cause, shortfall.reason),
        cause=cause,
    )


def cli_backend_wording(cause: BackendUnavailable, reason: str) -> str:
    """The CLI's one line for ``cause``: what is missing, and what ``--backend`` may name."""
    fact = backend_unavailable_text(cause)
    allowed = ", ".join(cause.declared) or "any backend"
    return f"{fact}: {reason}. --backend may only name a backend the task declares ({allowed})."


def _invalid_settings(settings: SettingsInvalid) -> PreflightReport:
    return PreflightReport(
        settings=settings,
        uses_docker=None,
        agent_image=_not_checked(CheckId.AGENT_IMAGE),
        evaluator=_not_checked(CheckId.EVALUATOR_AVAILABLE),
    )


def valid_report(
    *,
    uses_docker: bool,
    bundle: InputBundle,
    probe: CommandProbe,
    roles: tuple[str, ...],
    backend: Backend,
) -> PreflightReport:
    """The report for valid settings; the agent image is checked only where Docker runs."""
    return PreflightReport(
        settings=SettingsValid(),
        uses_docker=uses_docker,
        agent_image=agent_image_check(probe) if uses_docker else NO_AGENT_IMAGE,
        evaluator=evaluator_check(bundle),
        backend=backend,
        roles=roles,
    )


NO_AGENT_IMAGE = Check(
    id=CheckId.AGENT_IMAGE,
    state=CheckState.OK,
    detail="not needed: this run's environment starts no Docker container",
)
"""The agent image check for a run whose environment needs no Docker: nothing to build."""


def agent_image_check(probe: CommandProbe) -> Check:
    """``ok`` when an agent image was built here before, else a ``warn`` that one will be."""
    result = probe.run(AGENT_IMAGES_COMMAND, timeout_seconds=DOCKER_TIMEOUT_SECONDS)
    if result is None or result.returncode != 0:
        return Check(
            id=CheckId.AGENT_IMAGE,
            state=CheckState.WARN,
            detail="not checked: Docker did not list its images",
        )
    if result.stdout.strip():
        return Check(
            id=CheckId.AGENT_IMAGE,
            state=CheckState.OK,
            detail="an agent image is cached here; a new build reuses its layers",
        )
    return Check(
        id=CheckId.AGENT_IMAGE,
        state=CheckState.WARN,
        detail="the agent image will be built when the run starts (a first build takes minutes)",
    )


def evaluator_check(bundle: InputBundle) -> Check:
    """``ok`` naming the task's evaluator: its source, else its benchmark command.

    It fails, with a ``declare_build`` fix, when the evaluator requires candidate build
    outputs the task declares no build for: every measurement would fail its check.
    """
    missing = bundle.missing_build
    if missing is not None:
        return Check(
            id=CheckId.EVALUATOR_AVAILABLE,
            state=CheckState.FAIL,
            detail=one_line(missing, "the task declares no build"),
            fix=DeclareBuild(manifest=str(bundle.manifest_path), outputs=bundle.candidate_outputs),
        )
    source = bundle.evaluator_path
    # The run writes the project's path into the command for each candidate; the check is of
    # this project, so it names that path, not the unexpanded placeholder.
    command = bundle.benchmark_command_display.replace(PROJECT_ROOT_TOKEN, str(bundle.root))
    detail = f"evaluator source {source}" if source is not None else f"benchmark command {command}"
    return Check(
        id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.OK, detail=one_line(detail, "available")
    )


def _not_checked(check: CheckId) -> Check:
    return Check(id=check, state=CheckState.WARN, detail="not checked: the settings are invalid")
