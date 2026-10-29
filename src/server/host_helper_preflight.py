"""The host helper's typed run options and preflight checks, as pure decisions.

``option_arguments`` turns ``RunOptions`` into the run's own flags (the ones
``server.host_api`` names, which the CLI parses), so the screen's choices and a terminal's
flags have one meaning. ``repeated_option`` refuses ``args`` that repeat a typed option,
and ``unvetted_role`` a role model the run's harness does not vet. ``agents`` (a vetted pair per
role) becomes the run's per-role provider and model flags the same way; ``unvetted_agent``,
``conflicting_agent`` and ``undeclared_agent`` refuse a pair that is not vetted, a role model
that ``options`` also sets, and a role the run does not declare. ``preflight_checks``
assembles the closed ``Check`` list from what the helper reads (its checkout, the project's
repository, Docker) and the ``PreflightReport`` the run's own code printed. Light like the
rest of the helper.

Public surface: ``option_arguments``, ``option_flags``, ``repeated_option``,
``unvetted_role``, ``unvetted_agent``, ``conflicting_agent``, ``undeclared_agent``,
``preflight_checks``, ``measurement_check``, ``measurement_fits``, ``typed_arguments``, ``refined_cause``, ``PreflightFacts``,
``preflight_unavailable``.
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
    ROLE_PROVIDER_FLAG,
    ROLE_REASONING_EFFORT_FLAG,
    HarnessChoice,
    HarnessOption,
    RoleAgents,
)
from server.host_compute import (
    ComputeSettings,
    MeasurementTarget,
    ModalTarget,
    SameMachine,
    SlurmTarget,
)
from server.host_machine_facts import (
    Accelerator,
    DockerReachable,
    DockerUnreachable,
    MachineCheckout,
)
from server.host_preflight import (
    ArgumentsUnrecognized,
    Backend,
    BackendUnavailable,
    Cause,
    Check,
    CheckId,
    CheckState,
    ConfigureCompute,
    CopyOut,
    MeasurementTargetMisfit,
    MeasurementTargetNotConfigured,
    OptionNotAccepted,
    PreflightReport,
    ProjectDirty,
    ReasoningEffort,
    RolesNotDeclared,
    RunOption,
    RunOptions,
    RunResults,
    SettingsInvalid,
    StartDocker,
    cause_text,
    one_line,
)

if TYPE_CHECKING:
    from pathlib import Path

_RESULTS_FLAGS = {RunResults.KEEP: LOCAL_FLAG, RunResults.PUBLISH_GITHUB: PUBLISH_FLAG}


def typed_arguments(
    options: RunOptions | None, agents: RoleAgents | None = None
) -> tuple[tuple[RunOption, tuple[str, ...]], ...]:
    """Each option set in ``options`` and role agent in ``agents`` with the run arguments that carry it.

    The one place an option becomes flags: ``option_arguments`` flattens it, and a failure the
    run reports about an argument is traced back to the option that produced it.
    """
    pairs: list[tuple[RunOption, tuple[str, ...]]] = []
    for role, choice in (agents or {}).items():
        pairs.append((RunOption.ROLE_AGENT, (ROLE_PROVIDER_FLAG, f"{role}={choice.provider}")))
        pairs.append((RunOption.ROLE_AGENT, (ROLE_MODEL_FLAG, f"{role}={choice.model}")))
    if options is None:
        return tuple(pairs)
    if options.rounds is not None:
        pairs.append((RunOption.ROUNDS, (MAX_ROUNDS_FLAG, str(options.rounds))))
    if options.time_limit_s is not None:
        pairs.append((RunOption.TIME_LIMIT, (MAX_RUN_SECONDS_FLAG, str(options.time_limit_s))))
    if options.results is not None:
        pairs.append((RunOption.RESULTS, (_RESULTS_FLAGS[options.results],)))
    for role in options.roles:
        if role.model is not None:
            pairs.append((RunOption.ROLE_MODEL, (ROLE_MODEL_FLAG, f"{role.role}={role.model}")))
        if role.reasoning_effort is not ReasoningEffort.DEFAULT:
            pairs.append(
                (
                    RunOption.ROLE_REASONING_EFFORT,
                    (ROLE_REASONING_EFFORT_FLAG, f"{role.role}={role.reasoning_effort}"),
                )
            )
    return tuple(pairs)


def option_arguments(
    options: RunOptions | None, agents: RoleAgents | None = None
) -> tuple[str, ...]:
    """The run flags that carry ``options`` and ``agents``; none for an option left unset."""
    return tuple(
        argument
        for _option, arguments in typed_arguments(options, agents)
        for argument in arguments
    )


def refined_cause(
    cause: Cause, options: RunOptions | None, agents: RoleAgents | None = None
) -> Cause:
    """``cause``, naming the typed option when the run rejected the arguments that carry it.

    The run reports arguments its search does not take; only the helper knows which of them it
    wrote for a typed option, so the screen can point at that control instead of Extra arguments.
    """
    if isinstance(cause, ArgumentsUnrecognized):
        for option, arguments in typed_arguments(options, agents):
            if arguments[0] in cause.arguments:
                return OptionNotAccepted(option=option)
    return cause


def option_flags(options: RunOptions | None, agents: RoleAgents | None = None) -> tuple[str, ...]:
    """The run flags ``args`` may not hold next to ``options`` and ``agents``: those of each set."""
    flags: list[str] = [ROLE_PROVIDER_FLAG, ROLE_MODEL_FLAG] if agents else []
    if options is None:
        return tuple(flags)
    if options.rounds is not None:
        flags.append(MAX_ROUNDS_FLAG)
    if options.time_limit_s is not None:
        flags.append(MAX_RUN_SECONDS_FLAG)
    if options.results is not None:
        flags += [LOCAL_FLAG, PUBLISH_FLAG, REPO_FLAG]
    if options.roles:
        flags += [ROLE_MODEL_FLAG, ROLE_REASONING_EFFORT_FLAG]
    return tuple(dict.fromkeys(flags))


def repeated_option(
    args: tuple[str, ...], options: RunOptions | None, agents: RoleAgents | None = None
) -> str | None:
    """Why ``args`` repeat a typed option, naming the argument and its flag, or ``None``."""
    for token in args:
        for flag in option_flags(options, agents):
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


def unvetted_agent(agents: RoleAgents | None, harnesses: tuple[HarnessOption, ...]) -> str | None:
    """Why a role's agent is not a pair ``harnesses`` offers, naming its key, or ``None``."""
    for role, choice in (agents or {}).items():
        if any((h.provider, h.model) == (choice.provider, choice.model) for h in harnesses):
            continue
        vetted = [option.model for option in harnesses if option.provider == choice.provider]
        return (
            f"agents.{role}: model {choice.model!r} is not vetted for provider "
            f"{choice.provider!r}; vetted models: {', '.join(vetted) or 'none'}"
        )
    return None


def undeclared_agent(agents: RoleAgents | None, report: PreflightReport) -> str | None:
    """Why ``agents`` name a role the run does not declare, naming its key, or ``None``.

    The run's own validation reports the undeclared roles of its configuration, and the helper
    wrote the ``agents`` roles into that configuration, so a role both name is the helper's.
    """
    settings = report.settings
    if not agents or not isinstance(settings, SettingsInvalid):
        return None
    if not isinstance(settings.cause, RolesNotDeclared):
        return None
    unknown = [role for role in agents if role in settings.cause.roles]
    if not unknown:
        return None
    keys = ", ".join(f"agents.{role}" for role in unknown)
    return f"{keys}: the run's orchestration declares no such role"


def conflicting_agent(options: RunOptions | None, agents: RoleAgents | None) -> str | None:
    """Why a role's agent and its typed model both choose its model, naming both keys, or ``None``."""
    if options is None or not agents:
        return None
    for role in options.roles:
        if role.model is not None and role.role in agents:
            return (
                f"agents.{role.role} sets the model of role {role.role!r}, which "
                "options.roles also sets; set it in one place"
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


def preflight_checks(
    facts: PreflightFacts,
    report: PreflightReport,
    *,
    options: RunOptions | None = None,
    agents: RoleAgents | None = None,
    with_causes: bool = False,
) -> tuple[Check, ...]:
    """Every check, in ``CheckId`` order.

    ``options`` are the typed options the run was asked with, and ``with_causes`` says the client
    reads the typed cause of a failed ``settings_valid`` (feature ``run_causes``), else that
    check keeps the run's own message and no cause.
    """
    return (
        _checkout_check(facts),
        _repository_check(facts),
        _clean_check(facts, report),
        _settings_check(report, options, agents, with_causes=with_causes),
        _docker_check(facts.docker) if report.uses_docker is not False else _NO_DOCKER,
        report.agent_image,
        report.evaluator,
    )


_REQUIRED_ACCELERATOR = {
    Backend.CUDA: Accelerator.CUDA,
    Backend.ROCM: Accelerator.ROCM,
    Backend.METAL: Accelerator.METAL,
}
"""The accelerator each GPU backend needs; ``cpu`` runs anywhere and ``trainium`` on none here."""


def measurement_fits(backend: Backend, offered: Accelerator) -> bool:
    """Whether a place whose GPUs run work as ``offered`` can run a task targeting ``backend``."""
    if backend is Backend.CPU:
        return True
    return _REQUIRED_ACCELERATOR.get(backend) is offered


def measurement_check(
    target: MeasurementTarget,
    settings: ComputeSettings,
    backend: Backend | None,
) -> Check:
    """The ``measurement_target`` check: ``target`` is set up on the machine and fits the task.

    ``backend`` is the compute backend the run resolved to (``None`` when the settings
    resolved none). The same machine is checked by the run's own validation, so it passes here.
    A place whose hardware is not stated passes with a warning; one set up and known not to fit
    fails.
    """
    if isinstance(target, SameMachine):
        return Check(
            id=CheckId.MEASUREMENT_TARGET,
            state=CheckState.OK,
            detail="results are measured on this machine",
        )
    if isinstance(target, SlurmTarget):
        listed = next(
            (each for each in settings.clusters if each.cluster.name == target.cluster), None
        )
        if listed is None:
            return _not_configured(target)
        hardware = listed.cluster.hardware
        offered = None if hardware is None else hardware.accelerator
        place = f"cluster {target.cluster!r}"
    else:
        if not settings.modal.token_present:
            return _not_configured(target)
        offered, place = Accelerator.CUDA, "Modal"
    if backend is None or offered is None:
        why = "the task's backend is not known" if backend is None else "its hardware is not stated"
        return Check(
            id=CheckId.MEASUREMENT_TARGET,
            state=CheckState.WARN,
            detail=one_line(f"{place} is set up; fit not checked: {why}", "not checked"),
        )
    if not measurement_fits(backend, offered):
        cause = MeasurementTargetMisfit(target=target, backend=backend, offered=offered)
        return Check(
            id=CheckId.MEASUREMENT_TARGET,
            state=CheckState.FAIL,
            detail=one_line(f"{place}: {cause.plain()}", "does not fit"),
            cause=cause,
        )
    return Check(
        id=CheckId.MEASUREMENT_TARGET,
        state=CheckState.OK,
        detail=one_line(f"{place} offers the task's {backend.value} backend", "fits"),
    )


def _not_configured(target: SlurmTarget | ModalTarget) -> Check:
    cause = MeasurementTargetNotConfigured(target=target)
    return Check(
        id=CheckId.MEASUREMENT_TARGET,
        state=CheckState.FAIL,
        detail=one_line(cause.plain(), "not set up"),
        fix=ConfigureCompute(target=target),
        cause=cause,
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


def _settings_check(
    report: PreflightReport,
    options: RunOptions | None,
    agents: RoleAgents | None,
    *,
    with_causes: bool,
) -> Check:
    settings = report.settings
    if not isinstance(settings, SettingsInvalid):
        return Check(
            id=CheckId.SETTINGS_VALID,
            state=CheckState.OK,
            detail="the run's configuration is valid",
        )
    cause = settings.cause
    if isinstance(cause, BackendUnavailable) or (cause is not None and with_causes):
        # A typed cause is worded here without flags (the CLI's message names them); the app
        # words the cause itself for its own controls.
        cause = refined_cause(cause, options, agents)
        return Check(
            id=CheckId.SETTINGS_VALID,
            state=CheckState.FAIL,
            detail=one_line(cause_text(cause), settings.code),
            cause=cause,
        )
    return Check(
        id=CheckId.SETTINGS_VALID,
        state=CheckState.FAIL,
        detail=one_line(settings.message, settings.code),
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
