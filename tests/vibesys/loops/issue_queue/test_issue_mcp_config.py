"""Tests for :func:`build_issue_mcp_spec`.

The spec builder is the only issue-tracker-specific piece of the MCP path;
everything else (file format, file path, install/uninstall) lives in the
driver. These tests verify that the per-phase policy params are encoded
correctly into the spec's command-line args, and that the builder goes
through the host's generic tool-serving hook
(``vs_agent.expose_as_tools`` + ``vibesys.orchestration.tools.mcp_spec_from_descriptor``)
instead of hand-building an ``MCPServerSpec``.
"""

from __future__ import annotations

from vibesys.loops.issue_queue.loop import build_issue_mcp_spec
from vibesys.orchestration.tools import mcp_spec_from_descriptor
from vs_agent.api import MCPServerSpec, expose_as_tools
from vs_issue_board.api import IssueType


def test_build_judge_spec_has_correct_shape():  # noqa: ANN201  # tracked: #288
    spec = build_issue_mcp_spec(
        store_relpath="issues.json",
        creator="judge",
        iteration=3,
        cap=1,
        allowed_types={IssueType.BUG},
    )
    assert isinstance(spec, MCPServerSpec)
    assert spec.name == "vibesys-issues"
    assert spec.command == "python"
    # Args are forwarded to the standalone server's argparse CLI.
    assert spec.args == (
        "-m",
        "vs_issue_board.mcp",
        "issues.json",
        "--creator",
        "judge",
        "--iteration",
        "3",
        "--allowed-types",
        "bug",
        "--cap",
        "1",
    )
    assert spec.env == ()


def test_build_perf_eval_spec_sorts_allowed_types_alphabetically():  # noqa: ANN201  # tracked: #288
    spec = build_issue_mcp_spec(
        store_relpath="issues.json",
        creator="perf_eval",
        iteration=2,
        cap=3,
        allowed_types={IssueType.BUG, IssueType.FEATURE, IssueType.PERF},
    )
    args = spec.args
    assert args[0:2] == ("-m", "vs_issue_board.mcp")
    assert args[2] == "issues.json"
    assert args[args.index("--creator") + 1] == "perf_eval"
    assert args[args.index("--iteration") + 1] == "2"
    assert args[args.index("--cap") + 1] == "3"
    assert args[args.index("--allowed-types") + 1] == "bug,feature,perf"


def test_build_spec_omits_cap_flag_when_none():  # noqa: ANN201  # tracked: #288
    spec = build_issue_mcp_spec(
        store_relpath="issues.json",
        creator="agent",
        iteration=1,
        cap=None,
        allowed_types={IssueType.BUG, IssueType.FEATURE, IssueType.PERF},
    )
    # cap=None means "no cap" — the flag is absent so the server falls back
    # to its default of unlimited.
    assert "--cap" not in spec.args


def test_build_spec_uses_provided_store_relpath():  # noqa: ANN201  # tracked: #288
    spec = build_issue_mcp_spec(
        store_relpath="custom/path/issues.json",
        creator="judge",
        iteration=1,
        cap=1,
        allowed_types={IssueType.BUG},
    )
    assert "custom/path/issues.json" in spec.args
    assert spec.args[spec.args.index("custom/path/issues.json") - 1] == "vs_issue_board.mcp"


def test_build_spec_matches_generic_tool_serving_hook():  # noqa: ANN201  # tracked: #288
    """The board's spec must be exactly what the generic host-level hook produces.

    ``build_issue_mcp_spec`` no longer constructs ``MCPServerSpec`` itself: it
    builds a ``vs_agent.expose_as_tools`` descriptor (module + argv, the same
    generic shape ``vs_agent.mcp_server`` serves in the subprocess) and hands
    it to ``vibesys.orchestration.tools.mcp_spec_from_descriptor``, the one
    place that turns such a descriptor into a driver-facing spec.
    """
    spec = build_issue_mcp_spec(
        store_relpath="issues.json",
        creator="judge",
        iteration=3,
        cap=1,
        allowed_types={IssueType.BUG},
    )
    expected = mcp_spec_from_descriptor(
        expose_as_tools(
            name="vibesys-issues",
            entrypoint_module="vs_issue_board.mcp",
            entrypoint_args=(
                "issues.json",
                "--creator",
                "judge",
                "--iteration",
                "3",
                "--allowed-types",
                "bug",
                "--cap",
                "1",
            ),
        )
    )
    assert spec == expected
