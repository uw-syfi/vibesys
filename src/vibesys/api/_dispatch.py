"""Per-`LoopKind` dispatch from a `RunRequest` to the underlying loop function.

Private to `vibesys.api`. Each `_dispatch_*` helper routes `RunRequest`
plus the facts already carried by `RunRequest.input_bundle` into the
corresponding `vibesys.loops.{agent,evolve,plain}.loop` implementation, and
returns its bare `bool` success result unchanged. The loop functions are
imported lazily inside each helper, matching the lazy-import-for-startup-cost pattern already
used at their sole other call site, `entrypoints/cli.py`.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Literal

from vibesys.api.contracts import LoopKind
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError

if TYPE_CHECKING:
    from vibesys.api.contracts import RunRequest
    from vibesys.run.integration import LocalRunIntegration


def _required_objective(request: RunRequest) -> str:
    """Return `request.objective`, which agent/evolve requests must set.

    `RunRequest.objective` is `str | None` only because `_dispatch_plain`
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
                message=f"RunRequest for outer loop {request.loop.value!r} must set objective",
            )
        )
    return request.objective


def _agent_outer_loop(loop: LoopKind) -> Literal["agent", "profile-guided"]:
    """Narrow `loop` to the two values `dispatch_loop` routes to `_dispatch_agent`.

    `dispatch_loop` calls `_dispatch_agent` only for `LoopKind.AGENT`/
    `LoopKind.PROFILE_GUIDED` (see its routing below); this makes that
    invariant explicit here instead of letting `loop.value`'s plain `str`
    widen silently past `run_agent_loop`'s `outer_loop` literal.
    """
    if loop is LoopKind.PROFILE_GUIDED:
        return "profile-guided"
    if loop is not LoopKind.AGENT:
        message = f"_dispatch_agent called with unsupported loop kind: {loop!r}"
        raise TypeError(message)
    return "agent"


def resolved_run_id(request: RunRequest) -> str:
    """Return the run id the loop should use: the resume target, or `exp_name`."""
    if request.resume is not None:
        return request.resume.run_id
    if request.exp_name is None:
        message = "RunRequest.exp_name must be set for a fresh (non-resume) run"
        raise ValueError(message)
    return request.exp_name


def dispatch_loop(request: RunRequest, integration: LocalRunIntegration) -> bool:
    """Call the loop function selected by `request.loop` and return its success bool."""
    if request.loop in (LoopKind.AGENT, LoopKind.PROFILE_GUIDED):
        return _dispatch_agent(request, integration)
    if request.loop is LoopKind.PLAIN:
        return _dispatch_plain(request, integration)
    return _dispatch_evolve(request, integration)


def _dispatch_agent(request: RunRequest, integration: LocalRunIntegration) -> bool:
    run_agent_loop = import_module("vibesys.loops.agent.loop").run_agent_loop
    _required_objective(request)
    return run_agent_loop(request, integration=integration)


def _dispatch_evolve(request: RunRequest, integration: LocalRunIntegration) -> bool:
    run_evolve_loop = import_module("vibesys.loops.evolve.loop").run_evolve_loop
    return run_evolve_loop(request, integration=integration)


def _dispatch_plain(request: RunRequest, integration: LocalRunIntegration) -> bool:
    run_plain_loop = import_module("vibesys.loops.plain.loop").run_plain_loop
    return run_plain_loop(request=request, integration=integration)
