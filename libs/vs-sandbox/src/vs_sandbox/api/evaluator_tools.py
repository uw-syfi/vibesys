"""Immutable evaluator-tool installation for trusted sandbox composition."""

from vs_sandbox.evaluator_tools import (
    CargoGitToolSpec,
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
    ToolResult,
    cargo_install_argv,
    evaluator_tools_install_command,
    prepare_evaluator_tools,
    resolve_docker_image_id,
    tool_install_root,
    tool_path_replacements,
    tool_result,
    tool_spec_digest,
    tool_timed_out,
    tool_token,
)

__all__ = [
    "CargoGitToolSpec",
    "EvaluatorToolError",
    "EvaluatorToolLifecycleHooks",
    "ToolResult",
    "cargo_install_argv",
    "evaluator_tools_install_command",
    "prepare_evaluator_tools",
    "resolve_docker_image_id",
    "tool_install_root",
    "tool_path_replacements",
    "tool_result",
    "tool_spec_digest",
    "tool_timed_out",
    "tool_token",
]
