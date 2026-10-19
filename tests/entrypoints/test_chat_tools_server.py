"""Direct tests for the experiment-chat read-only MCP entrypoint.

Build the tool list, serve it from a `FastMCP` built by
`vs_mcp.api.tool_server`, and call tools through
`FastMCP.call_tool`. We do not test the stdio JSON-RPC framing or
`main`/`serve_stdio`; those are `mcp`/`vs_agent`'s responsibility.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pytest
from tests.support.multi_search import SearchRecord
from tests.support.real_git import REAL_GIT

from entrypoints.chat_tools_server import (
    PROPOSE_STEER_TOOL,
    READ_TOOLS,
    build_parser,
    build_tools,
)
from launch import open_run_store
from vibesys.api import (
    RunStatus,
    RunView,
    SteerMode,
    SteerProposal,
    SteerProposalReceipt,
    SteerTarget,
    SteerTargetKind,
)
from vibesys.api.hypothesis import RoundRecord
from vibesys.hypothesis import OrchestratorPlan
from vibesys.hypothesis.state import Hypothesis, HypothesisReview, HypothesisState
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vs_mcp.api import tool_server
from vs_project.api import (
    AgentRoleExecutionRecord,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.server.fastmcp import FastMCP

    from vibesys.api.store import RunStore
    from vs_mcp.api import ToolSpec

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


pytestmark = pytest.mark.xdist_group("chat_tools_server")


class TestArgparse:
    def test_help_uses_the_subprocess_module_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args(["--help"])

        assert exc_info.value.code == 0
        assert capsys.readouterr().out.startswith("usage: vibesys-run-mcp ")

    def test_requires_run_id_and_project_root(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_parses_run_id_and_project_root(self) -> None:
        args = build_parser().parse_args(["--run-id", "run-1", "--project-root", "/opt/project"])

        assert args.run_id == "run-1"
        assert args.project_root == "/opt/project"


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
                target={"kind": "role", "id": "dynamic-implementer"},
                mode="standing",
            )
        )

        receipt = SteerProposalReceipt.model_validate_json(out)
        assert receipt.proposal == SteerProposal(
            text="keep the weights in bf16",
            target=SteerTarget(kind=SteerTargetKind.ROLE, id="dynamic-implementer"),
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
