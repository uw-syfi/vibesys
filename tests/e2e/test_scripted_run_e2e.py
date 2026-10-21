"""An agent-less run: the real backend, scripted agents, no model.

The run is built from the command line as an operator builds it (``--agent-backend scripted``) and
goes through the production launcher and host composition: real Git worktrees, the runtime loop and
the trusted evaluation scripts, on the executing Fake Slurm cluster. Nothing is substituted but the
cluster, the state stores and the agents, which are the scripted ones. The cluster answers in
process (a connector process per request cost a third of the run), and the state stores are the
Fake that commits and fences as the product's do: a run commits over a thousand times, each commit
of the on-disk store decodes the whole document, and that was half of the run. What the run proves
does not depend on durability, which the state-store contract and durability tests cover. The
project is the harness's small CPU one (``queue.py`` holds the throughput), so the scenario runs in
seconds; the scripted agents get a workload of their own for it, because their built-in one edits
the Rust queue example.
The edits of that example are built and measured natively by ``test_scripted_queue_recipes_e2e``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tests.e2e.dynamic_loop_harness import _AGENT_CONFIG, LoopInput, resume_request, run_request

from launch.scripted_agents import ScriptedDynamicAgents
from vibesys.api import ParkRequest, RunStatus
from vibesys.events import CoreEventType, WorkstreamFinishedData
from vibesys.orchestration.dynamic.scripted.api import (
    CHAT_REPLY,
    IMPLEMENTER_KIND,
    JUDGE_KIND,
    PLANNER_KIND,
    EditKind,
    Recipe,
    Workload,
)
from vs_agent.api import ScriptedAgentClient, ScriptedClientSetup
from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.api import RunHandle
    from vs_agent.api import AgentClientProtocol, AgentEventSink, AgentSpec

ROUNDS = 3
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


def _scripted_client(
    *, spec: AgentSpec, events: AgentEventSink, log_dir: Path | None = None, **_services: object
) -> AgentClientProtocol:
    """The scripted client over this scenario's workload, as the launcher's factory builds it."""
    setup = ScriptedClientSetup(
        responders=ScriptedDynamicAgents(
            seed=spec.scripted_seed, sink=events, workloads=(VALUE_WORKLOAD,)
        ).responders(),
        chat=CHAT_REPLY,
        provider=spec.provider,
        model=spec.model,
        event_sink=events,
        log_dir=log_dir,
    )
    return ScriptedAgentClient(setup, threads=OsThreads())


class _NoAgents:
    """Stands where the harness expects its Fake clients: the scripted factory replaces it."""

    def client(self, **_kwargs: object) -> AgentClientProtocol:
        message = "an agent-less run builds its clients with the scripted factory"
        raise AssertionError(message)


def test_scripted_agents_plan_edit_and_judge_through_the_real_backend(tmp_path: Path) -> None:
    loop_input = LoopInput.create(
        tmp_path, agent_config=f"{_AGENT_CONFIG}[agent]\nscripted_seed = {SEED}\n"
    )
    request = loop_input.request(agent_backend="scripted", max_rounds=ROUNDS, max_in_flight=2)

    run = run_request(
        request,
        _NoAgents(),
        client_factory=_scripted_client,
        slurm_processes=loop_input.connector,
        state_stores=loop_input.state_stores,
        verify=False,
    )

    run.raise_error()
    assert run.status is RunStatus.COMPLETED
    finished = [e for e in run.events if e.type is CoreEventType.INVOCATION_FINISHED]
    kinds = {event.agent_kind for event in finished}
    assert {PLANNER_KIND, IMPLEMENTER_KIND, JUDGE_KIND} <= kinds
    types = [event.type for event in run.events]
    assert types.count(CoreEventType.ROUND_FINISHED) >= 1
    # Some workstreams end measured, some end for a reason: a lost turn, a judge's rejection.
    ended = [
        event.data.last_error
        for event in run.events
        if isinstance(event.data, WorkstreamFinishedData)
    ]
    assert None in ended
    assert any(error is not None for error in ended)
    # The agents narrate their turns, so a frontend has something to show for each.
    assert CoreEventType.AGENT_OUTPUT_CHUNK in types


def test_a_scripted_run_parks_cleanly_and_resumes_the_saved_search(tmp_path: Path) -> None:
    loop_input = LoopInput.create(
        tmp_path, agent_config=f"{_AGENT_CONFIG}[agent]\nscripted_seed = {SEED}\n"
    )
    request = loop_input.request(agent_backend="scripted", max_rounds=ROUNDS, max_in_flight=2)

    def park_on_start(handle: RunHandle) -> None:
        handle.session.submit(ParkRequest(control_id="park-before-start"))

    parked = run_request(
        request,
        _NoAgents(),
        client_factory=_scripted_client,
        slurm_processes=loop_input.connector,
        state_stores=loop_input.state_stores,
        on_handle=park_on_start,
        verify=False,
    )
    parked.raise_error()
    assert parked.status is RunStatus.PARKED
    types = [event.type for event in parked.events]
    assert CoreEventType.PARK_REQUESTED in types
    assert CoreEventType.PARKED in types
    assert CoreEventType.RUN_FAILED not in types
    assert CoreEventType.STOPPED not in types
    resumed = run_request(
        resume_request(request, parked.run_id),
        _NoAgents(),
        client_factory=_scripted_client,
        slurm_processes=loop_input.connector,
        state_stores=loop_input.state_stores,
        verify=False,
    )
    resumed.raise_error()
    assert resumed.status is RunStatus.COMPLETED
    assert resumed.run_id == parked.run_id
    assert any(event.type is CoreEventType.INVOCATION_FINISHED for event in resumed.events)
