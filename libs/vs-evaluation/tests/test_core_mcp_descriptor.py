"""The core evaluation tool server starts wherever its descriptor says to start it."""

from __future__ import annotations

from vs_evaluation.api.tools import core_evaluation_mcp_descriptor


def test_the_descriptor_leaves_the_interpreter_and_import_roots_to_the_launcher() -> None:
    descriptor = core_evaluation_mcp_descriptor("t", "/s")

    assert descriptor.command == "python"
    assert descriptor.env == ()
    assert dict(descriptor.runtime_env) == {
        "VS_EVALUATION_SOCKET": "/s",
        "VS_EVALUATION_TOKEN": "t",
    }
