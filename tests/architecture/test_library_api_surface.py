"""The public import surfaces of the reusable libraries resolve fully."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "vs_evaluator_protocol.api",
        "vs_feature_flags.api",
        "vs_github.api",
        "vs_issue_board.api",
        "vs_issue_board.api.mcp",
        "vs_loop_state.api",
        "vs_project.api",
        "vs_prompts.api",
        "vs_sandbox.api",
    ],
)
def test_library_public_exports_resolve(module_name: str) -> None:
    """A documented facade must expose every name it advertises."""
    api = importlib.import_module(module_name)
    exports = api.__all__

    assert exports
    for name in exports:
        assert getattr(api, name) is not None, f"{module_name}.{name}"
