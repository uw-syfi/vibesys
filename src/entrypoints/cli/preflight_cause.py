"""A run's configuration failure as the typed cause ``vibesys preflight`` reports.

``vibesys.api`` owns the facts a configuration failure can carry (``ConfigurationCause``);
``server.host_preflight`` owns the wire cause the desktop app words. This maps one to the other,
exhaustively, and gives a failure that has no typed cause the cause its stage implies, if any.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

import server.host_preflight as wire
import vibesys.api as facts

if TYPE_CHECKING:
    from vibesys.api import ConfigurationDiagnostic

ARGUMENT_STAGES = frozenset({"argument_parsing", "semantic_validation"})
"""The stages whose failures are about the arguments a run was started with, not about its inputs.

The desktop app starts a run with the arguments its controls produce, so a failure at one of
these stages is, in practice, in what the person typed as Extra arguments. The ones that have no
typed cause of their own are reported as ``ArgumentsRejected``.
"""


def wire_cause(cause: facts.ConfigurationCause) -> wire.Cause:
    """``cause`` as the wire cause: the same fact, in the contract the app reads."""
    wired: wire.Cause
    match cause:
        case (
            facts.TaskNotFound()
            | facts.TaskUnusable()
            | facts.TaskNeedsProvisioning()
            | facts.TaskImageUnsupported()
        ):
            wired = _task_cause(cause)
        case facts.ConfigInvalid():
            wired = wire.ConfigInvalid(problem=wire.clip(cause.problem))
        case facts.RolesNotDeclared():
            wired = wire.RolesNotDeclared(roles=_clipped(cause.roles))
        case facts.ModelNotVetted():
            wired = wire.ModelNotVetted(
                provider=wire.clip(cause.provider),
                model=None if cause.model is None else wire.clip(cause.model),
                vetted=_clipped(cause.vetted),
                role=None if cause.role is None else wire.clip(cause.role),
                fallback=cause.fallback,
            )
        case facts.ArgumentsRejected():
            wired = wire.ArgumentsRejected(problem=wire.clip(cause.problem))
        case facts.ArgumentsUnrecognized():
            wired = wire.ArgumentsUnrecognized(arguments=_clipped(cause.arguments))
        case facts.GitHubUnavailable():
            wired = wire.GitHubUnavailable(problem=wire.clip(cause.problem))
        case _:
            assert_never(cause)
    return wired


def _task_cause(
    cause: (
        facts.TaskNotFound
        | facts.TaskUnusable
        | facts.TaskNeedsProvisioning
        | facts.TaskImageUnsupported
    ),
) -> wire.Cause:
    """The wire cause of a failure about the task the run was asked to start."""
    wired: wire.Cause
    match cause:
        case facts.TaskNotFound():
            wired = wire.TaskNotFound(
                task=wire.clip(cause.task), available=_clipped(cause.available)
            )
        case facts.TaskUnusable():
            wired = wire.TaskUnusable(
                task=None if cause.task is None else wire.clip(cause.task),
                problem=wire.clip(cause.problem),
            )
        case facts.TaskNeedsProvisioning():
            wired = wire.TaskNeedsProvisioning(task=wire.clip(cause.task))
        case facts.TaskImageUnsupported():
            wired = wire.TaskImageUnsupported(
                task=None if cause.task is None else wire.clip(cause.task),
                problem=wire.clip(cause.problem),
            )
        case _:
            assert_never(cause)
    return wired


def settings_cause(diagnostic: ConfigurationDiagnostic) -> wire.Cause | None:
    """The cause of ``diagnostic``: its typed one, else the one its stage implies, else none."""
    if diagnostic.cause is not None:
        return wire_cause(diagnostic.cause)
    if diagnostic.stage in ARGUMENT_STAGES:
        return wire.ArgumentsRejected(problem=wire.clip(diagnostic.message))
    return None


def _clipped(texts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(wire.clip(each) for each in texts)
