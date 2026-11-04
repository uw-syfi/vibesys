"""A role's own provider and model reach that role's agent start, through the production host.

The run is built from the command line as an operator builds it, with roles on another provider
and model than the run's (``agent.roles.ROLE.provider`` and ``.model``), and goes through the
production launcher and host composition with the scripted agents. Each role's agent execution
starts on the provider and model its own entry chose, and a role with no entry on the run's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.e2e.dynamic_loop_harness import _AGENT_CONFIG, LoopInput, run_request
from tests.e2e.scripted_value_run import VALUE_WORKLOAD

from launch.scripted_agents import ScriptedDynamicAgents
from vibesys.api import vetted_models
from vibesys.dynamic_roles import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.events import AgentExecutionStartedData, CoreEventType
from vs_agent.api import ScriptedAgentClient, ScriptedClientSetup
from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol, AgentEventSink, AgentSpec

SEED = 1
RUN_PROVIDER = "codex"
OTHER_PROVIDER = "claude"
IMPLEMENTER_MODEL = vetted_models(OTHER_PROVIDER)[1]
JUDGE_MODEL = vetted_models(RUN_PROVIDER)[1]


def _scripted_client(
    *, spec: AgentSpec, events: AgentEventSink, log_dir: Path | None = None, **_services: object
) -> AgentClientProtocol:
    """One provider's scripted client over this scenario's workload, as the launcher builds it."""
    setup = ScriptedClientSetup(
        responders=ScriptedDynamicAgents(
            seed=spec.scripted_seed, sink=events, workloads=(VALUE_WORKLOAD,)
        ).responders(),
        chat="ok",
        provider=spec.provider,
        model=spec.model,
        event_sink=events,
        log_dir=log_dir,
    )
    return ScriptedAgentClient(setup, threads=OsThreads())


def test_each_role_starts_its_agent_on_the_provider_and_model_its_own_entry_chose(
    tmp_path: Path,
) -> None:
    roles = (
        f"[agent]\nscripted_seed = {SEED}\n"
        f'[agent.roles."{IMPLEMENTER.id}"]\nprovider = "{OTHER_PROVIDER}"\n'
        f'model = "{IMPLEMENTER_MODEL}"\n'
        # A role on the run's own provider changes its model only.
        f'[agent.roles."{JUDGE.id}"]\nmodel = "{JUDGE_MODEL}"\n'
    )
    loop_input = LoopInput.create(tmp_path, agent_config=f"{_AGENT_CONFIG}{roles}")
    request = loop_input.request(
        agent_backend="scripted", cli_provider=RUN_PROVIDER, max_rounds=1, max_in_flight=1
    )

    run = run_request(
        request,
        client_factory=_scripted_client,
        slurm_processes=loop_input.connector,
    )

    run.raise_error()
    started: dict[str, set[tuple[str | None, str | None]]] = {}
    for event in run.events:
        if event.type is CoreEventType.AGENT_EXECUTION_STARTED:
            assert isinstance(event.data, AgentExecutionStartedData)
            started.setdefault(event.agent_kind or "", set()).add(
                (event.data.provider, event.data.model)
            )
    # Every role started its agent, each on exactly the pair its own entry chose.
    assert started == {
        IMPLEMENTER.id: {(OTHER_PROVIDER, IMPLEMENTER_MODEL)},
        JUDGE.id: {(RUN_PROVIDER, JUDGE_MODEL)},
        ORCHESTRATOR.id: {(RUN_PROVIDER, vetted_models(RUN_PROVIDER)[0])},
    }
