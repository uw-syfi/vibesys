from __future__ import annotations

import socket
from itertools import pairwise
from pathlib import Path

from vs_sandbox.api import (
    EnvironmentBindMount,
    HostResource,
    HostResourceAccess,
    HostResourceContext,
    HostSandbox,
    SandboxKind,
    declare_resources,
    deduplicate_host_resources,
    host_resource_for_mount,
)
from vs_sandbox.api.testing import FakeComputeBackend


def test_host_sandbox_creates_parent_directories_for_imported_sockets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    socket_root = tmp_path / "runtime" / "ssh-agent"
    socket_root.mkdir(parents=True)
    socket_path = socket_root / "agent.sock"
    with socket.socket(socket.AF_UNIX) as agent_socket:
        agent_socket.bind(str(socket_path))
        sandbox = HostSandbox(
            workspace=workspace,
            bwrap_path="/usr/bin/bwrap",
            read_paths=(socket_path,),
        )

        command = sandbox.wrap(["true"])

    pairs = list(pairwise(command))
    assert ("--dir", str(socket_root)) in pairs
    assert ("--ro-bind-try", str(socket_path)) in pairs


def test_declaration_sdk_collects_resources_without_importing_them(tmp_path: Path) -> None:
    toolchain = tmp_path / "toolchain"
    context = HostResourceContext(env={"PROFILE": "test"})

    def declare(ctx: HostResourceContext) -> tuple[HostResource, ...]:
        assert ctx == context
        return (HostResource(toolchain, purpose="test toolchain"),)

    additional = HostResource(
        Path("/opt/model-cache"),
        HostResourceAccess.READ_WRITE,
        purpose="model cache",
    )

    assert declare_resources(
        context,
        (declare,),
        additional=(additional,),
    ) == (
        HostResource(toolchain, purpose="test toolchain"),
        additional,
    )


def test_environment_bind_mount_defaults_to_read_only(tmp_path: Path) -> None:
    assert EnvironmentBindMount(tmp_path / "model", "/model") == EnvironmentBindMount(
        host_path=tmp_path / "model",
        container_path="/model",
        read_only=True,
    )


def test_mount_lowering_preserves_access_purpose_and_identity_mapping(tmp_path: Path) -> None:
    host_path = tmp_path / "cache"

    remapped = host_resource_for_mount(
        host_path,
        "/opt/cache",
        read_only=False,
        purpose="build cache",
    )
    identity = host_resource_for_mount(
        host_path,
        str(host_path),
        read_only=True,
    )

    assert remapped == HostResource(
        host_path,
        HostResourceAccess.READ_WRITE,
        "build cache",
        "/opt/cache",
    )
    assert identity == HostResource(host_path, HostResourceAccess.READ_ONLY, "container mount")


def test_later_resource_wins_without_reordering_the_visible_path(tmp_path: Path) -> None:
    first = HostResource(Path("/opt/tool"))
    unrelated = HostResource(tmp_path / "other", agent_path="/opt/other")
    replacement = HostResource(
        tmp_path / "new",
        HostResourceAccess.READ_WRITE,
        agent_path="/opt/tool",
    )

    assert deduplicate_host_resources((first, unrelated, replacement)) == (
        replacement,
        unrelated,
    )


def test_normalized_resources_flow_through_the_public_backend_fake(tmp_path: Path) -> None:
    replacement = host_resource_for_mount(
        tmp_path / "replacement",
        "/opt/tool",
        read_only=False,
    )
    resources = deduplicate_host_resources(
        (
            host_resource_for_mount(tmp_path / "original", "/opt/tool", read_only=True),
            replacement,
        )
    )
    backend = FakeComputeBackend()

    backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path / "workspace"),
        resources=resources,
    )

    assert backend.creations[0].resources == (replacement,)
