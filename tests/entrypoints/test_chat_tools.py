"""Direct tests for the experiment-chat run tools.

Build the tool list, serve it from a `FastMCP` built by
`vs_mcp.api.tool_server`, and call tools through
`FastMCP.call_tool`. How the tools reach a chat agent is `test_chat_run_tools_in_container.py`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.multi_search import SearchRecord
from tests.support.real_git import REAL_GIT

from launch import built_in_orchestrations, open_run_store
from launch.agents import BuiltInSessionAgents
from launch.chat_tools import PROPOSE_STEER_TOOL, READ_TOOLS, build_tools
from vibesys.api import (
    AuxiliaryAgentLaunch,
    RunRecordFacts,
    RunStatus,
    RunView,
    SteerableAttempt,
    SteerMode,
    SteerProposal,
    SteerProposalReceipt,
    SteerTarget,
    SteerTargetKind,
    SteerTargets,
)
from vibesys.api.hypothesis import RoundRecord
from vibesys.api.testing import FakeRunRecord, FakeRunStore
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.hypothesis import OrchestratorPlan
from vibesys.hypothesis.state import Hypothesis, HypothesisReview, HypothesisState
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.run.integration import RunResources
from vs_agent.api import (
    CONTAINER_PYTHON,
    CONTAINER_SERVER_PACKAGES,
    AgentCapabilities,
    MCPServerSpec,
    NullAgentEventSink,
    containerize_server,
)
from vs_agent.api.testing import FakeAgentClient
from vs_evaluation.api.tool_relay import (
    SOCKET_ENV,
    TOKEN_ENV,
    RelayRefusedError,
    RelayUnavailableError,
    ToolRelayClient,
)
from vs_mcp.api import tool_server
from vs_project.api import (
    AgentRoleExecutionRecord,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)
from vs_runtime.api.infrastructure import RunEnvironmentSessionRole
from vs_sandbox.api import CommandResult, EnvironmentBindMount, ProjectPathPolicy
from vs_sandbox.api.testing import FakeCommandRunner
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from vibesys.api.store import RunStore
    from vs_agent.api import ToolResultPayload
    from vs_mcp.api import ToolServerDescriptor, ToolSpec

_ID = st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)
NOW = datetime(2026, 8, 11, 12, 34, 56, tzinfo=UTC)
UNIQUE = UUID("12345678-1234-5678-1234-567812345678")


def _configuration() -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=10,
        max_retries_per_round=3,
        judge_every=3,
        modality="text_generation",
        operator_constraints=("Do not change the ABI",),
    )


def _project_with_run(tmp_path: Path) -> tuple[Project, str]:
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    project = Project.open(tmp_path)
    project.state.create_project("Queue SPSC", now=NOW)
    manifest = project.state.new_run_manifest(
        "Queue SPSC",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=RunExecutionRecord(
            model="gpt-5",
            agent_backend="cli",
            cli_provider="codex",
            cli_timeout=1800,
            compute_backend="cpu",
            requested_profiler="linux-cpu",
            resolved_profiler="linux-cpu",
            default_reasoning_effort="high",
            agent_roles={
                "orchestrator": AgentRoleExecutionRecord(
                    model="gpt-5.6-sol", reasoning_effort="xhigh"
                ),
                "implementer": AgentRoleExecutionRecord(
                    model="gpt-5.6-luna", reasoning_effort="medium"
                ),
            },
        ),
        orchestration=OrchestrationDescriptor(
            id="multi-agent",
            config_version=1,
            options=_configuration().model_dump(mode="json"),
        ),
        trusted_input_baseline="a" * 40,
        now=NOW,
        unique=UNIQUE,
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="H-01",
        started_round=1,
        plan=OrchestratorPlan(
            hypothesis_id="H-01",
            title="Batch decode requests",
            hypothesis="Batching improves throughput.",
            task="Add request batching.",
            pass_criteria="",
            reasoning="",
        ),
        rounds=[
            RoundRecord(
                round_number=1,
                commit="c1",
                perf_metric=100.0,
                perf_unit="ops_s",
                perf_provenance="implementer",
                passed=True,
                hypothesis_id="H-01",
                judge_verdict="pass",
                hypothesis_outcome="proven",
            ),
        ],
        review=HypothesisReview.PASS,
    )


def _seed_agent_state(project: Project, run_id: str) -> None:
    SearchRecord(project, run_id).save(
        HypothesisState(hypotheses=[_hypothesis()], active_hypothesis_id="H-01")
    )


def _server(tools: tuple[ToolSpec, ...]) -> FastMCP:
    return tool_server(tools, server_name="vibesys-run-test")


async def _call(server: FastMCP, name: str, **kwargs: object) -> str:
    """Invoke an MCP tool and return its string result (see vs_issue_tracker's helper).

    ``FastMCP.call_tool`` is declared as returning
    ``Sequence[ContentBlock] | dict[str, Any]``, but for a tool with an
    output schema (every tool here, since each returns plain ``str``) its
    implementation actually returns a ``(content_blocks, structured_dict)``
    tuple; the declared return type just doesn't reflect that case (see
    ``vs_agent``'s ``test_tools.py::_call_tool``, the same cast). The cast
    documents the real, narrower shape instead of widening the result to
    ``Any``.
    """
    _, structured = cast("tuple[object, dict[str, Any]]", await server.call_tool(name, kwargs))
    return structured["result"]


def _tools(tmp_path: Path) -> tuple[tuple[ToolSpec, ...], str]:
    project, run_id = _project_with_run(tmp_path)
    _seed_agent_state(project, run_id)
    store = open_run_store(project, git=REAL_GIT, threads=SimThreads())
    return build_tools(store, run_id), run_id


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> tuple[FastMCP, tuple[ToolSpec, ...], str]:
    """One seeded run served by one server: the tools only read, so every test shares it."""
    tools, run_id = _tools(tmp_path_factory.mktemp("chat-tools"))
    return _server(tools), tools, run_id


pytestmark = pytest.mark.xdist_group("chat_tools")


class TestToolRegistration:
    def test_build_tools_registers_the_read_tools_and_propose_steer(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        _server_unused, tools, _run_id = seeded

        assert {tool.name for tool in tools} == {
            "run_summary",
            "list_ideas",
            "get_idea",
            "list_steps",
            "list_targets",
            "list_state_files",
            "read_state_file",
            "propose_steer",
        }

    def test_the_chat_agent_has_no_tool_that_sends(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        """The never-sends guarantee is structural: every tool reads, bar one that only proposes."""
        _server_unused, tools, _run_id = seeded

        proposing = {PROPOSE_STEER_TOOL}
        assert {tool.name for tool in tools} - proposing == set(READ_TOOLS)
        sending_words = ("send", "steer", "control", "withdraw", "stop", "pause", "cancel", "write")
        sending = [
            tool.name
            for tool in tools
            if tool.name not in proposing and any(word in tool.name for word in sending_words)
        ]
        assert sending == []

    def test_build_tools_fails_fast_on_an_unknown_run(self, tmp_path: Path) -> None:
        project, _run_id = _project_with_run(tmp_path)
        store = open_run_store(project, git=REAL_GIT, threads=SimThreads())

        with pytest.raises(Exception, match="no-such-run"):  # vs_project.ProjectStateError
            build_tools(store, "no-such-run")


class TestEndToEnd:
    def test_non_agent_projection_has_neutral_run_tools(self, tmp_path: Path) -> None:
        _project, run_id = _project_with_run(tmp_path)

        class CustomStore:
            def get_run(self, requested: str) -> RunView:
                assert requested == run_id
                return RunView(
                    run_id=run_id,
                    loop="team-search",
                    status=RunStatus.UNKNOWN,
                    projection={"kind": "team-search", "workers": 3},
                )

        tools = build_tools(cast("RunStore", CustomStore()), run_id)
        server = _server(tools)

        summary = asyncio.run(_call(server, "run_summary"))
        assert summary == f"run_id: {run_id}\nloop: team-search\nstatus: unknown"
        assert asyncio.run(_call(server, "list_ideas")) == "(no ideas)"
        assert asyncio.run(_call(server, "list_steps")) == "(no steps)"
        assert (
            asyncio.run(_call(server, "get_idea", idea_id="H-01"))
            == "(no idea 'H-01'; see list_ideas)"
        )

    def test_run_summary_reports_top_level_status(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, run_id = seeded

        out = asyncio.run(_call(server, "run_summary"))

        assert f"run_id: {run_id}" in out
        assert "loop: multi-agent" in out
        assert "active_idea_id: H-01" in out
        assert "idea_count: 1" in out
        assert "step_count: 1" in out

    def test_list_ideas_marks_the_active_one(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        out = asyncio.run(_call(server, "list_ideas"))

        assert "H-01 [active]" in out
        assert "Batch decode requests" in out
        assert "steps 1-1" in out

    def test_get_idea_returns_the_full_record(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        out = asyncio.run(_call(server, "get_idea", idea_id="H-01"))

        assert "claim: Batching improves throughput." in out
        assert "action: Add request batching." in out
        assert "step 1: passed=True" in out

    def test_get_idea_reports_an_unknown_id(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        out = asyncio.run(_call(server, "get_idea", idea_id="nope"))

        assert out == "(no idea 'nope'; see list_ideas)"

    def test_list_steps_is_run_wide(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        out = asyncio.run(_call(server, "list_steps"))

        assert "step 1: passed=True" in out
        assert "commit=c1" in out

    def test_list_state_files_includes_the_manifest_and_the_core_record(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        out = asyncio.run(_call(server, "list_state_files"))

        assert "run.json" in out
        assert "core-store/store.json" in out

    def test_read_state_file_returns_the_run_manifest_contents(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, run_id = seeded

        out = asyncio.run(_call(server, "read_state_file", relative_path="run.json"))

        assert run_id in out

    def test_read_state_file_reports_an_unknown_path(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        out = asyncio.run(_call(server, "read_state_file", relative_path="nope.json"))

        assert out == "(no state file 'nope.json'; see list_state_files)"


class TestProposeSteer:
    def test_returns_a_receipt_carrying_the_proposal_and_nothing_else(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        out = asyncio.run(
            _call(
                server,
                "propose_steer",
                text="keep the weights in bf16",
                target={"kind": "role", "id": "multi-implementer"},
                mode="standing",
            )
        )

        receipt = SteerProposalReceipt.model_validate_json(out)
        assert receipt.proposal == SteerProposal(
            text="keep the weights in bf16",
            target=SteerTarget(kind=SteerTargetKind.ROLE, id="multi-implementer"),
            mode=SteerMode.STANDING,
        )
        assert "cannot send it" in receipt.note

    def test_defaults_to_the_whole_run_at_its_next_turn(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        out = asyncio.run(_call(server, "propose_steer", text="profile the copy first"))

        proposal = SteerProposalReceipt.model_validate_json(out).proposal
        assert proposal.target == SteerTarget()
        assert proposal.mode is SteerMode.NEXT_TURN

    @pytest.mark.parametrize(
        "arguments",
        [
            {"text": ""},
            {"text": "go", "mode": "immediately"},
            {"text": "go", "target": {"kind": "run", "id": "dynamic-judge"}},
            {"text": "go", "target": {"kind": "role"}},
            {"text": "go", "target": {"kind": "planet", "id": "x"}},
            {"text": "go", "target": {"kind": "role", "id": "has space"}},
            {"text": "go", "control_id": "abc"},
        ],
    )
    def test_refuses_a_proposal_the_operators_send_would_refuse(
        self, seeded: tuple[FastMCP, tuple[ToolSpec, ...], str], arguments: dict[str, object]
    ) -> None:
        server, _tools_unused, _run_id = seeded

        with pytest.raises(Exception, match="propose_steer"):  # mcp ToolError names the tool
            asyncio.run(_call(server, "propose_steer", **arguments))


_TARGETS = SteerTargets(
    roles=("dynamic-planner", "dynamic-implementer"),
    attempts=(
        SteerableAttempt(attempt_id="attempt-1", item_id="item-1", ended=True),
        SteerableAttempt(attempt_id="attempt-2", item_id="item-2", ended=False),
    ),
    items=("item-1", "item-2"),
)


def _run_with(targets: SteerTargets | None) -> tuple[FastMCP, FakeRunRecord]:
    record = FakeRunRecord(
        run_id="run-1",
        run_view=RunView(run_id="run-1", loop="dynamic", status=RunStatus.ACTIVE),
        record_facts=RunRecordFacts(trusted_input_baseline="a" * 40),
        targets=targets,
    )
    return _server(build_tools(FakeRunStore({"run-1": record}), "run-1")), record


class TestSteerTargets:
    """A proposal names a target the run holds, by the ids the run's own state declares."""

    def test_list_targets_shows_the_ids_a_steer_may_name_and_which_attempts_ended(self) -> None:
        server, _record = _run_with(_TARGETS)

        out = asyncio.run(_call(server, "list_targets"))

        assert out == (
            "roles: dynamic-planner, dynamic-implementer\n"
            "attempts: attempt-1 (item item-1, ended), attempt-2 (item item-2)\n"
            "items: item-1, item-2"
        )

    def test_list_targets_says_so_for_a_run_with_no_committed_state(self) -> None:
        server, _record = _run_with(None)

        assert "no state yet" in asyncio.run(_call(server, "list_targets"))

    @pytest.mark.parametrize(
        "target",
        [
            {"kind": "run"},
            {"kind": "role", "id": "dynamic-planner"},
            {"kind": "attempt", "id": "attempt-2"},
            {"kind": "item", "id": "item-1"},
        ],
    )
    def test_a_target_the_run_holds_is_proposed(self, target: dict[str, str]) -> None:
        server, _record = _run_with(_TARGETS)

        out = asyncio.run(_call(server, "propose_steer", text="go", target=target))

        assert SteerProposalReceipt.model_validate_json(out).proposal.target == (
            SteerTarget.model_validate(target)
        )

    @pytest.mark.parametrize(
        ("target", "named"),
        [
            ({"kind": "role", "id": "dynamic-orchestrator"}, ["dynamic-planner"]),
            ({"kind": "attempt", "id": "attempt-9"}, ["attempt-2"]),
            ({"kind": "attempt", "id": "attempt-1"}, ["has ended", "attempt-2"]),
            ({"kind": "item", "id": "item-9"}, ["item-1", "item-2"]),
        ],
    )
    def test_a_target_the_run_would_refuse_is_refused_naming_it_and_the_valid_ids(
        self, target: dict[str, str], named: list[str]
    ) -> None:
        server, _record = _run_with(_TARGETS)

        with pytest.raises(Exception, match="propose_steer") as refused:
            asyncio.run(_call(server, "propose_steer", text="go", target=target))

        message = str(refused.value)
        assert repr(target["id"]) in message
        assert all(part in message for part in named)

    def test_a_run_with_no_committed_state_takes_only_a_steer_for_the_whole_run(self) -> None:
        server, _record = _run_with(None)

        asyncio.run(_call(server, "propose_steer", text="go"))
        with pytest.raises(Exception, match="no state yet"):
            asyncio.run(
                _call(server, "propose_steer", text="go", target={"kind": "role", "id": "x"})
            )

    def test_each_proposal_reads_the_run_as_it_stands_now(self) -> None:
        server, record = _run_with(_TARGETS)
        target = {"kind": "attempt", "id": "attempt-3"}
        with pytest.raises(Exception, match="attempt-3"):
            asyncio.run(_call(server, "propose_steer", text="go", target=target))

        record.targets = _TARGETS.model_copy(
            update={
                "attempts": (
                    *_TARGETS.attempts,
                    SteerableAttempt(attempt_id="attempt-3", item_id="item-3", ended=False),
                )
            }
        )

        asyncio.run(_call(server, "propose_steer", text="go", target=target))


@given(
    roles=st.lists(_ID, max_size=4, unique=True),
    attempts=st.lists(st.tuples(_ID, st.booleans()), max_size=4, unique_by=lambda row: row[0]),
    kind=st.sampled_from(list(SteerTargetKind)[1:]),
    name=_ID,
)
def test_a_proposal_is_accepted_exactly_when_the_targets_hold_its_target(
    roles: list[str], attempts: list[tuple[str, bool]], kind: SteerTargetKind, name: str
) -> None:
    targets = SteerTargets(
        roles=tuple(roles),
        attempts=tuple(
            SteerableAttempt(attempt_id=attempt, item_id=f"item-{attempt}", ended=ended)
            for attempt, ended in attempts
        ),
        items=tuple(f"item-{attempt}" for attempt, _ended in attempts),
    )
    server, _record = _run_with(targets)
    target = {"kind": kind.value, "id": name}
    allowed = name in targets.ids(kind)

    if allowed:
        asyncio.run(_call(server, "propose_steer", text="go", target=target))
    else:
        with pytest.raises(Exception, match="propose_steer"):
            asyncio.run(_call(server, "propose_steer", text="go", target=target))


# --- the chat agent in an agent container ---------------------------------------------------
#
# The container interpreter holds only the standard library, pydantic and mcp, and run state is
# hidden from the container, so the chat's `vibesys-run` tools run on the host and the
# container's tool server only forwards to them.


@dataclass(frozen=True)
class _EnvironmentRequest:
    workspace: Path
    log_dir: Path
    agent_homes_dir: Path | None = None
    environment_bind_mounts: tuple[EnvironmentBindMount, ...] = ()
    agent_backend: str | None = "cli"
    cli_provider: str | None = "codex"
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)
    session_role: RunEnvironmentSessionRole = RunEnvironmentSessionRole.ROOT


class _ContainerSession:
    """A run environment session whose agent runs in a container that `runner` executes in."""

    def __init__(self, runner: FakeCommandRunner, *, containerized: bool) -> None:
        self.sandbox = runner
        self.capture_commands = runner
        self.view = SimpleNamespace(cli_sandboxed=containerized, isolated=containerized)

    def close(self) -> None:
        """Nothing to release."""


@dataclass(frozen=True)
class _SessionOpener:
    request: _EnvironmentRequest
    session: _ContainerSession

    def open_session(self, _request: _EnvironmentRequest) -> _ContainerSession:
        return self.session


class _RecordingEvents(NullAgentEventSink):
    """The event stream a client reads: keeps what a tool result says."""

    def __init__(self) -> None:
        self.tool_results: list[tuple[str, str, bool, str | None]] = []

    def tool_result(  # noqa: PLR0913  # lint-waiver: LW-731905 [PLR0913]; this implements AgentEventSink.tool_result's named-argument contract exactly.
        self,
        tool: str,
        content: str,
        *,
        call_id: str | None = None,
        is_error: bool = False,
        payload: ToolResultPayload | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del call_id, payload, agent_kind, round_label
        self.tool_results.append((tool, content, is_error, invocation_id))


def _chat_resources(
    tmp_path: Path, project: Project, run_id: str, session: _ContainerSession, threads: SimThreads
) -> RunResources:
    workspace = tmp_path / "workspace"
    logs = tmp_path / "logs"
    workspace.mkdir()
    logs.mkdir()
    return RunResources(
        project_resources=cast(
            "Any", SimpleNamespace(project=project, state=SimpleNamespace(run_id=run_id))
        ),
        environment_resources=cast(
            "Any",
            _SessionOpener(_EnvironmentRequest(workspace=workspace, log_dir=logs), session),
        ),
        agent_backend="cli",
        provider="codex",
        model="gpt-6-luna",
        role_models=(),
        config=Config.model_validate(
            {"model": {"name": "gpt-6-luna"}, "agent": {"backend": "stub", "cli_provider": "codex"}}
        ),
        allow_unvetted_model=False,
        backend=ComputeBackend.CPU,
        skill_source_dirs=(),
        host_resources=(),
        clock=threads,
        threads=threads,
    )


_CHAT = AuxiliaryAgentLaunch(
    role="chat",
    member_id="default",
    provider="codex",
    model="gpt-6-luna",
    system_prompt="Investigate the run.",
)


class _ChatSession:
    """One chat conversation over a seeded run, opened as production opens it."""

    def __init__(
        self, tmp_path: Path, threads: SimThreads, *, containerized: bool, runner: FakeCommandRunner
    ) -> None:
        project, run_id = _project_with_run(tmp_path)
        _seed_agent_state(project, run_id)
        self.network = SimNetwork(threads)
        self.client = FakeAgentClient(
            threads=threads, capabilities=AgentCapabilities(tool_servers=True, session_reuse=True)
        )
        self.events = _RecordingEvents()
        agents = BuiltInSessionAgents(
            client_factory=lambda **_services: self.client,
            network=self.network,
            git=REAL_GIT,
            registry=built_in_orchestrations(),
        )
        resources = _chat_resources(
            tmp_path,
            project,
            run_id,
            _ContainerSession(runner, containerized=containerized),
            threads,
        )
        self.agent = agents.create_agent(_CHAT, resources, self.events)
        self.threads = threads

    def server(self) -> ToolServerDescriptor:
        """The tool server the agent was given for its first turn."""
        [descriptor] = self.client.calls[0].tool_servers or []
        return descriptor

    def relay_client(self, presented: str | None = None) -> ToolRelayClient:
        """What the container's tool server is: a client of the socket and token it was handed."""
        env = dict(self.server().runtime_env)
        return ToolRelayClient(
            Path(env[SOCKET_ENV]),
            env[TOKEN_ENV] if presented is None else presented,
            network=self.network,
            clock=self.threads,
        )


def test_a_containerized_chat_reads_the_run_through_its_vibesys_run_tool(tmp_path: Path) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[str, list[str], str, str]:
        chat = _ChatSession(
            tmp_path, threads, containerized=True, runner=FakeCommandRunner(threads=threads)
        )
        try:
            chat.agent.turn("how did the run go?", invocation_id="inv-1")
            relay = chat.relay_client()
            steps = relay.call("list_steps", {})
            summary = relay.call("run_summary", {})
            assert not steps.is_error
            assert not summary.is_error
            return (
                chat.server().name,
                [tool.name for tool in relay.list_tools()],
                steps.text,
                summary.text,
            )
        finally:
            chat.agent.close()

    name, tools, steps, summary = threads.run(main)

    assert name == "vibesys-run"
    assert tools == [*READ_TOOLS, PROPOSE_STEER_TOOL]
    assert "perf=100.0 ops_s" in steps
    assert "idea_count: 1" in summary


def test_the_chat_tool_server_starts_on_the_container_interpreter_and_its_packages(
    tmp_path: Path,
) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> MCPServerSpec:
        chat = _ChatSession(
            tmp_path, threads, containerized=True, runner=FakeCommandRunner(threads=threads)
        )
        try:
            chat.agent.turn("hello")
            server = chat.server()
            return MCPServerSpec(
                server.name, server.command, server.args, server.env, server.runtime_env
            )
        finally:
            chat.agent.close()

    started = containerize_server(threads.run(main))

    assert started.command == CONTAINER_PYTHON
    assert started.args[:2] == ("-m", "vs_evaluation.tool_relay_mcp")
    assert "vs_evaluation" in {name.replace("-", "_") for name in CONTAINER_SERVER_PACKAGES}


def test_the_chat_run_tools_are_refused_without_the_conversations_token(tmp_path: Path) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> None:
        chat = _ChatSession(
            tmp_path, threads, containerized=True, runner=FakeCommandRunner(threads=threads)
        )
        try:
            chat.agent.turn("hello")
            other_agent = chat.relay_client(presented="another-agents-guess")
            with pytest.raises(RelayRefusedError, match="token"):
                other_agent.list_tools()
            with pytest.raises(RelayRefusedError, match="token"):
                other_agent.call("read_state_file", {"relative_path": "run.json"})
        finally:
            chat.agent.close()

    threads.run(main)


def test_a_chat_tool_server_that_cannot_start_in_the_container_is_reported_to_the_client(
    tmp_path: Path,
) -> None:
    threads = SimThreads(schedule_seed=0)
    failure = "ModuleNotFoundError: No module named 'entrypoints'"

    def main() -> tuple[list[tuple[str, str, bool, str | None]], str, list[str]]:
        runner = FakeCommandRunner(
            threads=threads,
            default_result=CommandResult(output=failure, exit_code=1, stderr=failure),
        )
        chat = _ChatSession(tmp_path, threads, containerized=True, runner=runner)
        try:
            answer = chat.agent.turn("what did the run find?", invocation_id="inv-7")
            return chat.events.tool_results, answer, [call.command for call in runner.calls]
        finally:
            chat.agent.close()

    results, answer, commands = threads.run(main)

    [(tool, content, is_error, invocation)] = results
    assert (tool, is_error, invocation) == ("vibesys-run", True, "inv-7")
    assert "did not start" in content
    assert failure in content
    assert answer  # the turn still ran: the agent can answer without its tools
    [check] = commands
    assert f"{CONTAINER_PYTHON} -m vs_evaluation.tool_relay_mcp --check" in check


def test_a_passing_startup_check_is_silent_and_runs_once(tmp_path: Path) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[list[tuple[str, str, bool, str | None]], int]:
        runner = FakeCommandRunner(threads=threads)
        chat = _ChatSession(tmp_path, threads, containerized=True, runner=runner)
        try:
            for question in ("one", "two", "three"):
                chat.agent.turn(question)
            return chat.events.tool_results, len(runner.calls)
        finally:
            chat.agent.close()

    results, checks = threads.run(main)

    assert results == []
    assert checks == 1


def test_a_chat_on_the_host_runs_no_container_check_and_still_reads_the_run(
    tmp_path: Path,
) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[int, str]:
        runner = FakeCommandRunner(threads=threads)
        chat = _ChatSession(tmp_path, threads, containerized=False, runner=runner)
        try:
            chat.agent.turn("hello")
            return len(runner.calls), chat.relay_client().call("list_ideas", {}).text
        finally:
            chat.agent.close()

    checks, ideas = threads.run(main)

    assert checks == 0
    assert "H-01 [active]" in ideas


def test_closing_the_chat_stops_its_relay(tmp_path: Path) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> None:
        chat = _ChatSession(
            tmp_path, threads, containerized=True, runner=FakeCommandRunner(threads=threads)
        )
        chat.agent.turn("hello")
        relay = chat.relay_client()
        relay.ping()
        chat.agent.close()
        chat.agent.close()  # idempotent
        with pytest.raises(RelayUnavailableError):
            relay.ping()

    threads.run(main)
