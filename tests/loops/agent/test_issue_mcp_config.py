"""Tests for :func:`build_issue_mcp_spec`.

The spec builder is the only issue-tracker-specific piece of the MCP path —
everything else (file format, file path, install/uninstall) lives in
``vibe_serve/_agent_cli/``. These tests verify that the per-phase policy params
are encoded correctly into the spec's command-line args list.
"""

from __future__ import annotations

from vibe_serve._agent_cli.base import MCPServerSpec
from vibe_serve.loops.plain.mcp_config import build_issue_mcp_spec
from vs_issue_board import IssueType


def test_build_judge_spec_has_correct_shape():
    spec = build_issue_mcp_spec(
        store_relpath="issues.json",
        creator="judge",
        iteration=3,
        cap=1,
        allowed_types={IssueType.BUG},
    )
    assert isinstance(spec, MCPServerSpec)
    assert spec.name == "vibeserve-issues"
    assert spec.command == "python"
    # Args are forwarded to the standalone server's argparse CLI.
    assert spec.args == [
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
    ]
    assert spec.env == {}


def test_build_perf_eval_spec_sorts_allowed_types_alphabetically():
    spec = build_issue_mcp_spec(
        store_relpath="issues.json",
        creator="perf_eval",
        iteration=2,
        cap=3,
        allowed_types={IssueType.BUG, IssueType.FEATURE, IssueType.PERF},
    )
    args = spec.args
    assert args[0:2] == ["-m", "vs_issue_board.mcp"]
    assert args[2] == "issues.json"
    assert args[args.index("--creator") + 1] == "perf_eval"
    assert args[args.index("--iteration") + 1] == "2"
    assert args[args.index("--cap") + 1] == "3"
    assert args[args.index("--allowed-types") + 1] == "bug,feature,perf"


def test_build_spec_omits_cap_flag_when_none():
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


def test_build_spec_uses_provided_store_relpath():
    spec = build_issue_mcp_spec(
        store_relpath="custom/path/issues.json",
        creator="judge",
        iteration=1,
        cap=1,
        allowed_types={IssueType.BUG},
    )
    assert "custom/path/issues.json" in spec.args
    assert spec.args[spec.args.index("custom/path/issues.json") - 1] == "vs_issue_board.mcp"
