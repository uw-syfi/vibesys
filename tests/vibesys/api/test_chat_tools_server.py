"""Direct unit tests for `vibesys.api.chat_tools_server`'s read-only MCP tools.

Mirrors `vs_issue_board.mcp`'s own test pattern (see
`libs/vs-issue-board/tests/test_mcp.py`): build the tool list, register it on
a bare `FastMCP` via `vs_agent.mcp_server.register_tool`, and call tools
through `FastMCP.call_tool`. We do not test the stdio JSON-RPC framing or
`main`/`serve_stdio`; those are `mcp`/`vs_agent`'s responsibility.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pytest
from mcp.server.fastmcp import FastMCP

from vibesys.api import RunStatus, RunView
from vibesys.api.chat_tools_server import build_parser, build_tools
from vibesys.api.store import RunStore, open_run_store
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.state.api import RoundRecord
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.search.hypothesis.state import Hypothesis, HypothesisReview, HypothesisState
from vs_agent.api import register_tool
from vs_project.api import Project, RunEnvironmentRecord, RunExecutionRecord

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import ToolSpec

NOW = datetime(2026, 8, 11, 12, 34, 56, tzinfo=UTC)
UNIQUE = UUID("12345678-1234-5678-1234-567812345678")


def _configuration() -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=10,
        max_retries_per_round=3,
        judge_every=3,
        official_eval_every=3,
        memory_layout="files",
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
        run_environment=RunEnvironmentRecord(name="local"),
        execution=RunExecutionRecord(
            model="gpt-5",
            agent_backend="cli",
            agent_driver="agentshim",
            cli_provider="codex",
            cli_timeout=1800,
            compute_backend="cpu",
            requested_profiler="linux-cpu",
            resolved_profiler="linux-cpu",
            default_reasoning_effort="high",
            outer_model="gpt-5.6-sol",
            outer_reasoning_effort="xhigh",
            inner_model="gpt-5.6-luna",
            inner_reasoning_effort="medium",
        ),
        orchestration=descriptor_from_options(_configuration(), orchestration_id="multi-agent"),
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
                passed=True,
                hypothesis_id="H-01",
                judge_verdict="pass",
                hypothesis_outcome="proven",
            ),
        ],
        review=HypothesisReview.PASS,
    )


def _seed_agent_state(project: Project, run_id: str) -> None:
    portable = project.state.portable_namespace(run_id, "multi")
    portable.slot("state.json", HypothesisState).save(
        HypothesisState(hypotheses=[_hypothesis()], active_hypothesis_id="H-01")
    )


def _server(tools: tuple[ToolSpec, ...]) -> FastMCP:
    mcp = FastMCP("vibesys-run-test")
    for spec in tools:
        register_tool(mcp, spec)
    return mcp


async def _call(server: FastMCP, name: str, **kwargs: object) -> str:
    """Invoke an MCP tool and return its string result (see vs_issue_board's helper).

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
    store = open_run_store(project)
    return build_tools(store, project, run_id), run_id


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
    def test_build_tools_registers_the_six_read_only_tools(self, tmp_path: Path) -> None:
        tools, _run_id = _tools(tmp_path)

        assert {tool.name for tool in tools} == {
            "run_summary",
            "list_hypotheses",
            "get_hypothesis",
            "list_rounds",
            "list_state_files",
            "read_state_file",
        }

    def test_build_tools_fails_fast_on_an_unknown_run(self, tmp_path: Path) -> None:
        project, _run_id = _project_with_run(tmp_path)
        store = open_run_store(project)

        with pytest.raises(Exception, match="no-such-run"):  # vs_project.ProjectStateError
            build_tools(store, project, "no-such-run")


class TestEndToEnd:
    def test_non_agent_projection_has_neutral_run_tools(self, tmp_path: Path) -> None:
        project, run_id = _project_with_run(tmp_path)

        class CustomStore:
            def get_run(self, requested: str) -> RunView:
                assert requested == run_id
                return RunView(
                    run_id=run_id,
                    loop="team-search",
                    status=RunStatus.UNKNOWN,
                    projection={"kind": "team-search", "workers": 3},
                )

        tools = build_tools(cast("RunStore", CustomStore()), project, run_id)
        server = _server(tools)

        summary = asyncio.run(_call(server, "run_summary"))
        assert summary == f"run_id: {run_id}\nloop: team-search\nstatus: unknown"
        assert asyncio.run(_call(server, "list_hypotheses")) == "(no hypotheses)"
        assert asyncio.run(_call(server, "list_rounds")) == "(no rounds)"
        assert (
            asyncio.run(_call(server, "get_hypothesis", hypothesis_id="H-01"))
            == "(no hypothesis 'H-01'; see list_hypotheses)"
        )

    def test_run_summary_reports_top_level_status(self, tmp_path: Path) -> None:
        tools, run_id = _tools(tmp_path)
        server = _server(tools)

        out = asyncio.run(_call(server, "run_summary"))

        assert f"run_id: {run_id}" in out
        assert "loop: multi-agent" in out
        assert "active_hypothesis_id: H-01" in out
        assert "hypothesis_count: 1" in out
        assert "round_count: 1" in out

    def test_list_hypotheses_marks_the_active_one(self, tmp_path: Path) -> None:
        tools, _run_id = _tools(tmp_path)
        server = _server(tools)

        out = asyncio.run(_call(server, "list_hypotheses"))

        assert "H-01 [active]" in out
        assert "Batch decode requests" in out
        assert "rounds 1-1" in out

    def test_get_hypothesis_returns_the_full_record(self, tmp_path: Path) -> None:
        tools, _run_id = _tools(tmp_path)
        server = _server(tools)

        out = asyncio.run(_call(server, "get_hypothesis", hypothesis_id="H-01"))

        assert "claim: Batching improves throughput." in out
        assert "action: Add request batching." in out
        assert "round 1: passed=True" in out

    def test_get_hypothesis_reports_an_unknown_id(self, tmp_path: Path) -> None:
        tools, _run_id = _tools(tmp_path)
        server = _server(tools)

        out = asyncio.run(_call(server, "get_hypothesis", hypothesis_id="nope"))

        assert out == "(no hypothesis 'nope'; see list_hypotheses)"

    def test_list_rounds_is_run_wide(self, tmp_path: Path) -> None:
        tools, _run_id = _tools(tmp_path)
        server = _server(tools)

        out = asyncio.run(_call(server, "list_rounds"))

        assert "round 1: passed=True" in out
        assert "commit=c1" in out

    def test_list_state_files_includes_the_manifest_and_agent_state(self, tmp_path: Path) -> None:
        tools, _run_id = _tools(tmp_path)
        server = _server(tools)

        out = asyncio.run(_call(server, "list_state_files"))

        assert "run.json" in out
        assert "multi/state.json" in out

    def test_read_state_file_returns_the_run_manifest_contents(self, tmp_path: Path) -> None:
        tools, run_id = _tools(tmp_path)
        server = _server(tools)

        out = asyncio.run(_call(server, "read_state_file", relative_path="run.json"))

        assert run_id in out

    def test_read_state_file_reports_an_unknown_path(self, tmp_path: Path) -> None:
        tools, _run_id = _tools(tmp_path)
        server = _server(tools)

        out = asyncio.run(_call(server, "read_state_file", relative_path="nope.json"))

        assert out == "(no state file 'nope.json'; see list_state_files)"
