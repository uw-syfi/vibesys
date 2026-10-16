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

from entrypoints.cli.loops import validated_run_request
from launch import orchestration_roles
from server.host_preflight import (
    Check,
    CheckId,
    CheckState,
    PreflightReport,
    SettingsInvalid,
    SettingsValid,
    one_line,
)
from vibesys.api import ConfigurationError
from vibesys.api.request import (
    TaskImageError,
    run_accelerator_unavailable,
    run_environment_uses_docker,
)
from vs_agent.api.images import AGENT_IMAGE_REPOSITORY
from vs_sim.api import SubprocessProbe

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Callable

    from vibesys.api.request import InputBundle
    from vs_sim.api import CommandProbe

DOCKER_TIMEOUT_SECONDS = 5.0
AGENT_IMAGES_COMMAND = ("docker", "image", "ls", "--quiet", AGENT_IMAGE_REPOSITORY)
"""Lists the agent images built on this machine; Docker reuses their layers in a new build."""
ACCELERATOR_UNAVAILABLE = "accelerator_unavailable"
"""The settings code when this host lacks what the run checks before its image build.

That is the compute backend's accelerator, or the Sysbox runtime a docker-in-docker task needs.
"""
TASK_IMAGE_UNSUPPORTED = "task_image_unsupported"
"""The settings code when the run cannot honor the task's Dockerfile as asked."""


def run_preflight(argv: list[str], parse: Callable[[list[str]], Namespace]) -> None:
    """Print the report for a run started with ``argv``; ``parse`` parses as a run does."""
    report = preflight_report(argv, parse, SubprocessProbe())
    sys.stdout.write(report.model_dump_json() + "\n")


def preflight_report(
    argv: list[str], parse: Callable[[list[str]], Namespace], probe: CommandProbe
) -> PreflightReport:
    """Validate ``argv`` as a run would, then check the agent image and the evaluator."""
    try:
        args = parse(argv)
        request, _config = validated_run_request(args)
        roles = orchestration_roles(request.orchestration)
        accelerator = run_accelerator_unavailable(request, probe)
    except ConfigurationError as error:
        diagnostic = error.diagnostic
        return _invalid_settings(
            SettingsInvalid(
                code=diagnostic.code, stage=diagnostic.stage, message=diagnostic.message
            )
        )
    except TaskImageError as error:
        # The run refuses this before it builds anything; report it as settings.
        return _invalid_settings(
            SettingsInvalid(
                code=TASK_IMAGE_UNSUPPORTED, stage="run_environment", message=str(error)
            )
        )
    if accelerator is not None:
        # The run stops on this before its image build, seconds after it starts.
        return PreflightReport(
            settings=SettingsInvalid(
                code=ACCELERATOR_UNAVAILABLE, stage="compute_backend", message=accelerator
            ),
            uses_docker=run_environment_uses_docker(request.run_environment),
            agent_image=_not_checked(CheckId.AGENT_IMAGE),
            evaluator=_not_checked(CheckId.EVALUATOR_AVAILABLE),
            roles=roles,
        )
    return valid_report(
        uses_docker=run_environment_uses_docker(request.run_environment),
        bundle=args.input_bundle,
        probe=probe,
        roles=roles,
    )


def _invalid_settings(settings: SettingsInvalid) -> PreflightReport:
    return PreflightReport(
        settings=settings,
        uses_docker=None,
        agent_image=_not_checked(CheckId.AGENT_IMAGE),
        evaluator=_not_checked(CheckId.EVALUATOR_AVAILABLE),
    )


def valid_report(
    *, uses_docker: bool, bundle: InputBundle, probe: CommandProbe, roles: tuple[str, ...]
) -> PreflightReport:
    """The report for valid settings; the agent image is checked only where Docker runs."""
    return PreflightReport(
        settings=SettingsValid(),
        uses_docker=uses_docker,
        agent_image=agent_image_check(probe) if uses_docker else NO_AGENT_IMAGE,
        evaluator=evaluator_check(bundle),
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
    """``ok`` naming the task's evaluator: its source, else its benchmark command."""
    source = bundle.evaluator_path
    detail = (
        f"evaluator source {source}"
        if source is not None
        else f"benchmark command {bundle.benchmark_command_display}"
    )
    return Check(
        id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.OK, detail=one_line(detail, "available")
    )


def _not_checked(check: CheckId) -> Check:
    return Check(id=check, state=CheckState.WARN, detail="not checked: the settings are invalid")
