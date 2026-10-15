"""Prepare trusted evaluator tools in their target Docker image."""

from __future__ import annotations

import shlex
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from vs_runtime._trusted_evaluation_preparation import (
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluatorRequirements,
    docker_evaluator_tools_root,
    evaluator_container_setup,
)
from vs_sandbox.api import (
    BeforeReadyContext,
    ComputeBackendImpl,
    HostResource,
    HostResourceAccess,
    SandboxKind,
    execute_as_root,
    start_sandbox,
    stop_sandbox,
)
from vs_sandbox.api.evaluator_tools import (
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
    prepare_evaluator_tools,
    tool_install_root,
)
from vs_sim.api import DISCARD

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_runtime._environment_startup import ToolBuildOutputOpener
    from vs_sim.api import OutputSink


#: Upper bound for one root setup command (an apt install with retries).
_SETUP_TIMEOUT_S = 1800


class _EvaluatorToolBuildRequiredError(RuntimeError):
    pass


def _require_builder(
    _arguments: Sequence[str],
) -> NoReturn:
    raise _EvaluatorToolBuildRequiredError


@dataclass(frozen=True)
class DockerToolBuilder:
    """Where a missing evaluator tool is built, and where the build's output goes.

    A build runs in an accelerator-free ephemeral sandbox of *backend* on
    *image*, the immutable target image the tools are cached for, with its
    workspace and log under *log_dir*. *output* opens the stream the build's
    commands write to; ``None`` discards it.
    """

    backend: ComputeBackendImpl
    log_dir: Path
    image: str
    output: ToolBuildOutputOpener | None = None


def prepare_docker_evaluator_resources(
    requirements: TrustedEvaluatorRequirements,
    workspace: Path,
    *,
    builder: DockerToolBuilder,
) -> tuple[HostResource, ...]:
    """Build missing evaluator tools and return verified read-only resources.

    Tool caches are keyed by the immutable target image. A cache miss starts
    one accelerator-free ephemeral Docker sandbox using that same image. The
    sandbox owns target-native installation, while this function restores host
    ownership, stops the builder on every path, and verifies every receipt
    before returning any resource. The build's command output goes to the
    stream the builder's ``output`` opens for it.
    """
    tools = requirements.tools
    if not tools:
        return ()
    host_parent = docker_evaluator_tools_root(
        requirements,
        workspace,
        image_identity=builder.image,
    )

    try:
        prepare_evaluator_tools(tools, host_parent, command_runner=_require_builder)
    except _EvaluatorToolBuildRequiredError:
        opened = (
            nullcontext(DISCARD) if builder.output is None else builder.output(tuple(sorted(tools)))
        )
        # The build's stream stays open until the build is verified, so a build that
        # publishes too little ends its stream as failed.
        with opened as output:
            _build_evaluator_tools(requirements, builder, host_parent, output)
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
    builder_spec: DockerToolBuilder,
    host_parent: Path,
    output: OutputSink,
) -> None:
    tools = requirements.tools
    log_dir = builder_spec.log_dir
    for name in tools:
        (host_parent / name).mkdir(parents=True, exist_ok=True)
    builder_workspace = log_dir / "evaluator-tool-builder-workspace"
    builder_workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".host-owner-", dir=host_parent) as marker:
        container_marker = str(SANDBOX_EVALUATOR_TOOLS_ROOT / Path(marker.name).name)
        builder = builder_spec.backend.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(builder_workspace),
            log_path=log_dir / "evaluator-tool-builder.log",
            bind_mounts=[
                (str(host_parent), str(SANDBOX_EVALUATOR_TOOLS_ROOT), False),
            ],
            extra_env={},
            attach_accelerator=False,
            ephemeral=True,
            container_image=builder_spec.image,
        )
        try:
            start_sandbox(builder, output=output)
            # The tool's root-only setup (system packages, toolchain) runs here,
            # as root in the started builder, before the tools are built. It is
            # an ordinary exec, so it works on every backend and shows in the
            # builder log.
            for command in evaluator_container_setup(requirements):
                setup = execute_as_root(builder, command, timeout=_SETUP_TIMEOUT_S, output=output)
                if setup.exit_code != 0:
                    detail = (setup.output or "setup command failed").strip()
                    raise EvaluatorToolError.builder_setup_failed(detail[-500:])
            EvaluatorToolLifecycleHooks(tools, SANDBOX_EVALUATOR_TOOLS_ROOT).before_ready(
                BeforeReadyContext(builder, output=output)
            )
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
                output=DISCARD,
            )
            if ownership.exit_code != 0:
                detail = (ownership.output or "chown failed").strip()
                raise EvaluatorToolError.cache_ownership_failed(detail[:500])
        finally:
            stop_sandbox(builder)
