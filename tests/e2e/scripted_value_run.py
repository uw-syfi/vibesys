"""The agent-less run's scripted agents over the harness's small CPU project, shared by the scripted e2e tests.

The scripted agents get a workload of their own (their built-in one edits the Rust queue
example): each recipe raises, lowers or breaks the ``VALUE`` that ``queue.py`` reports as
throughput, so an agent's edit decides every trusted outcome.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from launch.scripted_agents import ScriptedDynamicAgents
from vibesys.orchestration.dynamic.scripted.api import CHAT_REPLY, EditKind, Recipe, Workload
from vs_agent.api import ScriptedAgentClient, ScriptedClientSetup
from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol, AgentEventSink, AgentSpec

SEED = 1
_VALUE = re.compile(r"^VALUE = (-?\d+)$", re.MULTILINE)


def _set_value(change: int | None) -> Callable[[str], str | None]:
    """An edit of ``queue.py``: raise ``VALUE`` by ``change``, or make it negative for ``None``."""

    def edit(source: str) -> str | None:
        found = _VALUE.search(source)
        if found is None:
            return None
        value = -1 if change is None else int(found.group(1)) + change
        return _VALUE.sub(f"VALUE = {value}", source) + "# [scripted] edited: see the diff\n"

    return edit


def _recipe(slug: str, kind: EditKind, change: int | None, weight: int = 1) -> Recipe:
    return Recipe(
        slug=slug,
        kind=kind,
        title=f"Try {slug}",
        claim=f"The {slug} change moves throughput.",
        task=f"Apply the {slug} change.",
        evidence_wanted="The check passes and throughput rises.",
        summary=f"Applied the {slug} change to queue.py.",
        edit=_set_value(change),
        weight=weight,
    )


VALUE_WORKLOAD = Workload(
    name="value",
    source="queue.py",
    manifest="queue.py",
    manifest_text="VALUE",
    recipes=(
        _recipe("raise-value", EditKind.IMPROVES, 3, weight=3),
        _recipe("lower-value", EditKind.REGRESSES, -1),
        _recipe("negate-value", EditKind.BREAKS_ACCURACY, None),
    ),
)


def scripted_setup(
    *, spec: AgentSpec, events: AgentEventSink, log_dir: Path | None = None
) -> ScriptedClientSetup:
    """What this scenario's scripted agents answer with, as the launcher's factory builds it."""
    return ScriptedClientSetup(
        responders=ScriptedDynamicAgents(
            seed=spec.scripted_seed, sink=events, workloads=(VALUE_WORKLOAD,)
        ).responders(),
        chat=CHAT_REPLY,
        provider=spec.provider,
        model=spec.model,
        event_sink=events,
        log_dir=log_dir,
    )


def scripted_client(
    *, spec: AgentSpec, events: AgentEventSink, log_dir: Path | None = None, **_services: object
) -> AgentClientProtocol:
    """The scripted client over this scenario's workload, as the launcher's factory builds it."""
    setup = scripted_setup(spec=spec, events=events, log_dir=log_dir)
    return ScriptedAgentClient(setup, threads=OsThreads())


class NoAgents:
    """Stands where the harness expects its Fake clients: the scripted factory replaces it."""

    def client(self, **_kwargs: object) -> AgentClientProtocol:
        message = "an agent-less run builds its clients with the scripted factory"
        raise AssertionError(message)
