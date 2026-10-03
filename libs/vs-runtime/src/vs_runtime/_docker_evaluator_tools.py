"""Prepare trusted evaluator tools in their target Docker image."""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vs_runtime._trusted_evaluation_preparation import (
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluatorRequirements,
    docker_evaluator_tools_root,
    evaluator_container_setup,
)
from vs_sandbox.api import (
    ComputeBackendImpl,
    HostResource,
    HostResourceAccess,
    SandboxKind,
    start_sandbox,
    stop_sandbox,
)
from vs_sandbox.api.evaluator_tools import (
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
    prepare_evaluator_tools,
    tool_install_root,
)

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Sequence


class _EvaluatorToolBuildRequiredError(RuntimeError):
    pass


def _require_builder(
    _arguments: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    raise _EvaluatorToolBuildRequiredError


def prepare_docker_evaluator_resources(
    requirements: TrustedEvaluatorRequirements,
    workspace: Path,
    *,
    backend: ComputeBackendImpl,
    log_dir: Path,
    container_image: str,
) -> tuple[HostResource, ...]:
    """Build missing evaluator tools and return verified read-only resources.

    Tool caches are keyed by the immutable target image. A cache miss starts
    one accelerator-free ephemeral Docker sandbox using that same image. The
    sandbox owns target-native installation, while this function restores host
    ownership, stops the builder on every path, and verifies every receipt
    before returning any resource.
    """
    tools = requirements.tools
    if not tools:
        return ()
    host_parent = docker_evaluator_tools_root(
        requirements,
        workspace,
        image_identity=container_image,
    )

    try:
        prepare_evaluator_tools(tools, host_parent, command_runner=_require_builder)
    except _EvaluatorToolBuildRequiredError:
        _build_evaluator_tools(
            requirements,
            backend=backend,
            log_dir=log_dir,
            container_image=container_image,
            host_parent=host_parent,
        )
        try:
            prepare_evaluator_tools(tools, host_parent, command_runner=_require_builder)
        except _EvaluatorToolBuildRequiredError as exc:
            raise EvaluatorToolError.builder_incomplete() from exc

    return tuple(
        HostResource(
            tool_install_root(host_parent, name, spec),
            HostResourceAccess.READ_ONLY,
            "evaluator tool",
            str(tool_install_root(SANDBOX_EVALUATOR_TOOLS_ROOT, name, spec)),
        )
        for name, spec in tools.items()
    )


def _build_evaluator_tools(
    requirements: TrustedEvaluatorRequirements,
    *,
    backend: ComputeBackendImpl,
    log_dir: Path,
    container_image: str,
    host_parent: Path,
) -> None:
    tools = requirements.tools
    for name in tools:
        (host_parent / name).mkdir(parents=True, exist_ok=True)
    builder_workspace = log_dir / "evaluator-tool-builder-workspace"
    builder_workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".host-owner-", dir=host_parent) as marker:
        container_marker = str(SANDBOX_EVALUATOR_TOOLS_ROOT / Path(marker.name).name)
        builder = backend.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(builder_workspace),
            log_path=log_dir / "evaluator-tool-builder.log",
            bind_mounts=[
                (str(host_parent), str(SANDBOX_EVALUATOR_TOOLS_ROOT), False),
            ],
            extra_env={},
            extra_init_commands=evaluator_container_setup(requirements),
            lifecycle_hooks=[EvaluatorToolLifecycleHooks(tools, SANDBOX_EVALUATOR_TOOLS_ROOT)],
            attach_accelerator=False,
            ephemeral=True,
            container_image=container_image,
        )
        try:
            start_sandbox(builder)
            container_roots = [
                str(tool_install_root(SANDBOX_EVALUATOR_TOOLS_ROOT, name, spec))
                for name, spec in tools.items()
            ]
            ownership_script = (
                'owner=$(stat -c "%u:%g" -- "$1") && shift && chown -R "$owner" -- "$@"'
            )
            ownership = builder.execute(
                shlex.join(
                    (
                        "sh",
                        "-c",
                        ownership_script,
                        "vibesys-chown",
                        container_marker,
                        *container_roots,
                    )
                ),
                timeout=120,
            )
            if ownership.exit_code != 0:
                detail = (ownership.output or "chown failed").strip()
                raise EvaluatorToolError.cache_ownership_failed(detail[:500])
        finally:
            stop_sandbox(builder)
