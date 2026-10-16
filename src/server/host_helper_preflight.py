"""The host helper's typed run options and preflight checks, as pure decisions.

``option_arguments`` turns ``RunOptions`` into the run's own flags (the ones
``server.host_api`` names, which the CLI parses), so the screen's choices and a terminal's
flags have one meaning. ``repeated_option`` refuses ``args`` that repeat a typed option,
and ``unvetted_role`` a role model the run's harness does not vet. ``preflight_checks``
assembles the closed ``Check`` list from what the helper reads (its checkout, the project's
repository, Docker) and the ``PreflightReport`` the run's own code printed. Light like the
rest of the helper.

Public surface: ``option_arguments``, ``option_flags``, ``repeated_option``,
``unvetted_role``, ``preflight_checks``, ``PreflightFacts``, ``preflight_unavailable``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.host_api import (
    LOCAL_FLAG,
    MAX_ROUNDS_FLAG,
    MAX_RUN_SECONDS_FLAG,
    PUBLISH_FLAG,
    REPO_FLAG,
    ROLE_MODEL_FLAG,
    ROLE_REASONING_EFFORT_FLAG,
    DockerReachable,
    DockerUnreachable,
    HarnessChoice,
    HarnessOption,
    MachineCheckout,
)
from server.host_preflight import (
    Check,
    CheckId,
    CheckState,
    CopyOut,
    PreflightReport,
    ProjectDirty,
    ReasoningEffort,
    RunOptions,
    RunResults,
    SettingsInvalid,
    StartDocker,
    backend_unavailable_text,
    one_line,
)

if TYPE_CHECKING:
    from pathlib import Path

_RESULTS_FLAGS = {RunResults.KEEP: LOCAL_FLAG, RunResults.PUBLISH_GITHUB: PUBLISH_FLAG}


def option_arguments(options: RunOptions | None) -> tuple[str, ...]:
    """The run flags that carry ``options``; none for an option left unset."""
    if options is None:
        return ()
    arguments: list[str] = []
    if options.rounds is not None:
        arguments += [MAX_ROUNDS_FLAG, str(options.rounds)]
    if options.time_limit_s is not None:
        arguments += [MAX_RUN_SECONDS_FLAG, str(options.time_limit_s)]
    if options.results is not None:
        arguments.append(_RESULTS_FLAGS[options.results])
    for role in options.roles:
        if role.model is not None:
            arguments += [ROLE_MODEL_FLAG, f"{role.role}={role.model}"]
        if role.reasoning_effort is not ReasoningEffort.DEFAULT:
            arguments += [ROLE_REASONING_EFFORT_FLAG, f"{role.role}={role.reasoning_effort}"]
    return tuple(arguments)


def option_flags(options: RunOptions | None) -> tuple[str, ...]:
    """The run flags ``args`` may not hold next to ``options``: those of each option set."""
    if options is None:
        return ()
    flags: list[str] = []
    if options.rounds is not None:
        flags.append(MAX_ROUNDS_FLAG)
    if options.time_limit_s is not None:
        flags.append(MAX_RUN_SECONDS_FLAG)
    if options.results is not None:
        flags += [LOCAL_FLAG, PUBLISH_FLAG, REPO_FLAG]
    if options.roles:
        flags += [ROLE_MODEL_FLAG, ROLE_REASONING_EFFORT_FLAG]
    return tuple(flags)


def repeated_option(args: tuple[str, ...], options: RunOptions | None) -> str | None:
    """Why ``args`` repeat a typed option, naming the argument and its flag, or ``None``."""
    for token in args:
        for flag in option_flags(options):
            if abbreviates(token, flag):
                return f"{token} selects {flag}, which the typed options already set"
    return None


def abbreviates(token: str, flag: str) -> bool:
    """Whether argparse reads ``token`` (``--name`` or ``--name=value``) as ``flag``.

    Run options are parsed with argparse's default ``allow_abbrev``, so any
    unambiguous prefix of a long option selects it.
    """
    name = token.split("=", 1)[0]
    return name.startswith("--") and len(name) > len("--") and flag.startswith(name)


def unvetted_role(
    options: RunOptions | None,
    harness: HarnessChoice | None,
    harnesses: tuple[HarnessOption, ...],
) -> str | None:
    """Why a role's model is not one of the chosen harness's vetted models, or ``None``.

    Without a chosen harness the run's configured provider decides, and the run's own
    validation (``agent.roles.ROLE.model``) refuses an unvetted model by name.
    """
    if options is None or harness is None:
        return None
    vetted = [option.model for option in harnesses if option.provider == harness.provider]
    for role in options.roles:
        if role.model is not None and role.model not in vetted:
            return (
                f"role {role.role!r} model {role.model!r} is not vetted for provider "
                f"{harness.provider!r}; vetted models: {', '.join(vetted) or 'none'}"
            )
    return None


@dataclass(frozen=True)
class PreflightFacts:
    """What the helper read for one preflight, besides the run's own report."""

    project_root: Path
    repository_root: Path | None
    """The Git repository the project is in, ``None`` when it is in none."""
    checkout: MachineCheckout | None
    vibesys_version: str
    docker: DockerReachable | DockerUnreachable


def preflight_checks(facts: PreflightFacts, report: PreflightReport) -> tuple[Check, ...]:
    """Every check, in ``CheckId`` order."""
    return (
        _checkout_check(facts),
        _repository_check(facts),
        _clean_check(facts, report),
        _settings_check(report),
        _docker_check(facts.docker) if report.uses_docker is not False else _NO_DOCKER,
        report.agent_image,
        report.evaluator,
    )


def preflight_unavailable(detail: str) -> PreflightReport:
    """The report when ``vibesys preflight`` printed none: settings fail with ``detail``."""
    return PreflightReport(
        settings=SettingsInvalid(code="preflight_failed", stage="preflight", message=detail),
        uses_docker=None,
        agent_image=Check(
            id=CheckId.AGENT_IMAGE, state=CheckState.WARN, detail="not checked: preflight failed"
        ),
        evaluator=Check(
            id=CheckId.EVALUATOR_AVAILABLE,
            state=CheckState.WARN,
            detail="not checked: preflight failed",
        ),
    )


_NO_DOCKER = Check(
    id=CheckId.DOCKER_REACHABLE,
    state=CheckState.OK,
    detail="not needed: this run's environment starts no Docker container",
)


def _checkout_check(facts: PreflightFacts) -> Check:
    checkout = facts.checkout
    detail = (
        f"installed VibeSys {facts.vibesys_version}"
        if checkout is None
        else f"VibeSys {checkout.version} at {checkout.root}"
    )
    return Check(id=CheckId.CHECKOUT_FOUND, state=CheckState.OK, detail=one_line(detail, "found"))


def _repository_check(facts: PreflightFacts) -> Check:
    root, repository = facts.project_root, facts.repository_root
    if repository is None:
        detail = "not a Git repository yet; the run creates one"
        return Check(id=CheckId.PROJECT_OWN_REPOSITORY, state=CheckState.OK, detail=detail)
    if repository == root:
        return Check(
            id=CheckId.PROJECT_OWN_REPOSITORY,
            state=CheckState.OK,
            detail="the project is its own Git repository",
        )
    return Check(
        id=CheckId.PROJECT_OWN_REPOSITORY,
        state=CheckState.FAIL,
        detail=one_line(f"the project is inside the Git repository {repository}", "nested"),
        fix=CopyOut(suggested_path=str(repository.parent / root.name)),
    )


def _clean_check(facts: PreflightFacts, report: PreflightReport) -> Check:
    changes = report.project_changes
    if not changes:
        return Check(
            id=CheckId.PROJECT_CLEAN,
            state=CheckState.OK,
            detail="the project has no uncommitted changes",
        )
    cause = ProjectDirty(project=str(facts.project_root), paths=changes)
    return Check(
        id=CheckId.PROJECT_CLEAN,
        state=CheckState.FAIL,
        detail=one_line(
            f"{len(changes)} uncommitted path(s) in the project, starting with {changes[0]}",
            "uncommitted changes",
        ),
        cause=cause,
    )


def _settings_check(report: PreflightReport) -> Check:
    settings = report.settings
    if isinstance(settings, SettingsInvalid):
        # A typed cause is worded here without flags (the CLI's message names them); the app
        # words the cause itself for its own controls.
        message = (
            settings.message if settings.cause is None else backend_unavailable_text(settings.cause)
        )
        return Check(
            id=CheckId.SETTINGS_VALID,
            state=CheckState.FAIL,
            detail=one_line(message, settings.code),
            cause=settings.cause,
        )
    return Check(
        id=CheckId.SETTINGS_VALID,
        state=CheckState.OK,
        detail="the run's configuration is valid",
    )


def _docker_check(docker: DockerReachable | DockerUnreachable) -> Check:
    if isinstance(docker, DockerReachable):
        return Check(
            id=CheckId.DOCKER_REACHABLE,
            state=CheckState.OK,
            detail=one_line(f"Docker {docker.version}", "reachable"),
        )
    return Check(
        id=CheckId.DOCKER_REACHABLE,
        state=CheckState.FAIL,
        detail=one_line(docker.reason, "unreachable"),
        fix=StartDocker(),
    )
