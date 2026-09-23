"""Shared validation for the current loop adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vibesys.api._orchestrations.contracts import RunDescription
from vibesys.api.contracts import LoopKind
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.loops.roles import expected_agent_roles
from vibesys.profilers import ProfilerKind

if TYPE_CHECKING:
    from vibesys.api.contracts import AnyRunRequest, RunRequest


def _required_objective(request: RunRequest) -> str:
    """Return `request.objective`, which agent/evolve requests must set.

    `RunRequest.objective` is `str | None` because the plain adapter
    never reads it (a plain-loop request has no reason to set it). Every
    agent/evolve construction site sets it from `InputBundle.objective`
    (itself non-optional): `vibesys.api.entry.default_request` always passes
    `bundle.objective`, and `entrypoints/cli.py`'s
    `_build_agent_request`/`_build_evolve_request` always pass
    `bundle.objective` (optionally wrapped by `_with_operator_constraints`).
    Nothing in `RunRequest`'s own type ties `objective` to `loop`, so this
    turns that cross-field invariant into an explicit, checked contract
    instead of letting a future construction gap surface as a confusing
    `TypeError` inside the loop function.
    """
    if request.objective is None:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="missing_objective",
                stage="dispatch",
                message=f"RunRequest for outer loop {request.orchestration_id!r} must set objective",
            )
        )
    return request.objective


def _agent_outer_loop(loop: LoopKind | None) -> Literal["agent", "profile-guided"]:
    """Narrow `loop` to the two values registered for the agent adapter.

    The registry maps `LoopKind.AGENT` and `LoopKind.PROFILE_GUIDED` to the
    same adapter; this makes that
    invariant explicit here instead of letting `loop.value`'s plain `str`
    widen silently past `run_agent_loop`'s `outer_loop` literal.
    """
    if loop is LoopKind.PROFILE_GUIDED:
        return "profile-guided"
    assert loop is LoopKind.AGENT, (  # noqa: S101  # dispatch_loop only routes these two here
        f"agent adapter called with unsupported loop kind: {loop!r}"
    )
    return "agent"


def resolved_run_id(request: AnyRunRequest) -> str:
    """Return the run id the loop should use: the resume target, or `exp_name`."""
    if request.resume is not None:
        return request.resume.run_id
    if request.exp_name is None:
        message = "RunRequest.exp_name must be set for a fresh (non-resume) run"
        raise ValueError(message)
    return request.exp_name


def built_in_description(request: RunRequest, *, round_budget: bool) -> RunDescription:
    """Preserve a built-in policy's existing run-start presentation."""
    roles = expected_agent_roles(request.orchestration_id)
    if request.profiler_kind is ProfilerKind.NONE:
        roles = tuple(role for role in roles if role != "profiler")
    return RunDescription(
        max_rounds=(request.max_rounds if request.max_rounds is not None else 1)
        if round_budget
        else 1,
        expected_roles=roles,
    )
