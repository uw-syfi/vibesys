"""``vibesys.orchestration.tools``: the one adapter from a generic stdio tool
descriptor to a driver-facing ``MCPServerSpec``.

``tests/vibesys/loops/issue_queue/test_issue_mcp_config.py`` (a later stack
branch) exercises this indirectly through ``build_issue_mcp_spec``; these
tests cover ``mcp_spec_from_descriptor`` itself, the module's only function,
directly against ``vs_agent.expose_as_tools`` descriptors.
"""

from __future__ import annotations

from vibesys.orchestration.tools import mcp_spec_from_descriptor
from vs_agent.api import MCPServerSpec, expose_as_tools


def test_mcp_spec_from_descriptor_copies_fields_verbatim() -> None:
    descriptor = expose_as_tools(
        name="vibesys-issues",
        entrypoint_module="vs_issue_board.mcp",
        entrypoint_args=("issues.json", "--creator", "judge"),
        env={"TOKEN": "secret"},
    )

    spec = mcp_spec_from_descriptor(descriptor)

    assert isinstance(spec, MCPServerSpec)
    assert spec.name == descriptor.name == "vibesys-issues"
    assert spec.command == descriptor.command == "python"
    assert (
        spec.args
        == descriptor.args
        == ("-m", "vs_issue_board.mcp", "issues.json", "--creator", "judge")
    )
    assert spec.env == descriptor.env == (("TOKEN", "secret"),)


def test_mcp_spec_from_descriptor_defaults_to_no_args_or_env() -> None:
    descriptor = expose_as_tools(name="bare", entrypoint_module="pkg.mod")

    spec = mcp_spec_from_descriptor(descriptor)

    assert spec.args == ("-m", "pkg.mod")
    assert spec.env == ()


def test_mcp_spec_from_descriptor_honors_custom_command() -> None:
    descriptor = expose_as_tools(name="node-tool", entrypoint_module="server.js", command="node")

    spec = mcp_spec_from_descriptor(descriptor)

    assert spec.command == "node"
