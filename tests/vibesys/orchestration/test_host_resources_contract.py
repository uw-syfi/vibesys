"""Contract test: ``RunContext`` structurally satisfies ``HostResources``.

``HostResources`` (``vibesys.orchestration._host``) is a ``Protocol`` declared
at the base of the orchestration module graph purely to break an import
cycle: every capability module (``agents.py``, ``workspaces.py``, ``gates.py``,
``state.py``, ``environment.py``) types its private ``self._host`` reference
as ``HostResources`` instead of the real ``RunContext``, and imports it only
under ``TYPE_CHECKING`` (see ``_host.py``'s module docstring). That means
nothing imports it at runtime, so it is otherwise unreachable by coverage and
undetectable if ``RunContext`` drifts out of structural sync with it.

This test imports ``HostResources`` for real (defeating the TYPE_CHECKING
guard) and asserts a real, live ``RunContext`` (built through the existing
``run_with_context`` harness, the same one ``test_agents_turn.py`` uses)
provides every member the Protocol declares: properties, plain attributes,
and methods alike, gathered directly off the Protocol's own class namespace
rather than hand-duplicated, so a future member added to ``HostResources``
without a matching ``RunContext`` member fails this test immediately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.harness import run_with_context

from vibesys.orchestration._host import HostResources
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext

# Protocol machinery attributes that appear in HostResources' own namespace
# but are not members it declares (dunders, and the ABC/Protocol bookkeeping
# CPython attaches to every Protocol class).
_NOT_DECLARED_MEMBERS = {"_is_protocol", "_is_runtime_protocol", "_abc_impl"}


def _declared_members(protocol_cls: type) -> set[str]:
    """Return every attribute/method name a ``Protocol`` class declares.

    Combines plain annotated attributes (``__annotations__``, e.g.
    ``_setup: RunSetup``) with properties and methods defined directly on the
    class body (``request``, ``log``, ``_spawn``, ...), excluding dunders and
    Protocol/ABC bookkeeping so only members the author actually wrote remain.
    """
    members = set(protocol_cls.__annotations__)
    for name in vars(protocol_cls):
        if name.startswith("__") and name.endswith("__"):
            continue
        if name in _NOT_DECLARED_MEMBERS:
            continue
        members.add(name)
    return members


def test_host_resources_declares_a_nonempty_surface() -> None:
    """Guard the contract test itself against a Protocol that lost its body."""
    members = _declared_members(HostResources)

    assert members == {
        "request",
        "workspaces",
        "environment",
        "gates",
        "progress",
        "_setup",
        "_projector",
        "_gate_executor",
        "_parent_mutation_lock",
        "_spawn_lock",
        "_agents",
        "_resources",
        "events",
        "log",
        "warning",
        "_run_blocking",
        "_spawn",
    }


def test_run_context_provides_every_host_resources_member(tmp_path: Path) -> None:
    """A real ``RunContext`` structurally satisfies ``HostResources``.

    Fails the moment a capability module starts reading a ``ctx.host`` member
    that ``RunContext`` no longer provides under that name, without needing an
    import from this base-of-the-graph module back up to ``runtime.py``.
    """

    async def body(ctx: RunContext) -> None:
        missing = [name for name in _declared_members(HostResources) if not hasattr(ctx, name)]
        assert not missing, f"RunContext is missing HostResources member(s): {missing}"

    run_with_context(tmp_path, FakeAgentClient(backend_name="stub"), body)
