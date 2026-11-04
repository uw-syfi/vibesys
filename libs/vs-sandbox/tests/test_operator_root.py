"""No confinement lets the agent reach the operator's runtime root, which holds the control key.

The root holds the key a run's control socket demands (``vs_project.api.operator_key_path``).
Bubblewrap, Landlock and Docker can only grant, so each refuses a view that overlaps the
root; Seatbelt can subtract, so its profile denies the root after every allow. These tests
build each backend's view from its public constructor, with no real confinement.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import (
    DockerSandbox,
    HostSandbox,
    LandlockSandbox,
    SandboxUnavailableError,
    SeatbeltSandbox,
    exposed_grant,
)
from vs_sandbox.api.testing import ScriptedDockerCli
from vs_sim.api import DISCARD
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable

_SEGMENTS = st.lists(st.sampled_from(["a", "b", "run", "vibesys"]), min_size=1, max_size=4)


def _path(segments: list[str]) -> Path:
    return Path("/", "probe-root", *segments)


@given(root=_SEGMENTS, grant=_SEGMENTS)
def test_a_grant_overlaps_the_root_exactly_when_one_contains_the_other(
    root: list[str], grant: list[str]
) -> None:
    shorter, longer = sorted((root, grant), key=len)
    overlaps = longer[: len(shorter)] == shorter

    assert (exposed_grant(_path(root), [_path(grant)]) is not None) == overlaps


@given(root=_SEGMENTS, grants=st.lists(_SEGMENTS, max_size=5))
def test_the_first_overlapping_grant_is_named(root: list[str], grants: list[list[str]]) -> None:
    overlapping = [
        _path(grant) for grant in grants if exposed_grant(_path(root), [_path(grant)]) is not None
    ]

    assert exposed_grant(_path(root), [_path(grant) for grant in grants]) == (
        overlapping[0] if overlapping else None
    )


def test_a_symlink_into_the_root_overlaps_it(tmp_path: Path) -> None:
    root = tmp_path / "runtime" / "vibesys"
    root.mkdir(parents=True)
    link = tmp_path / "innocent"
    link.symlink_to(root / "runs", target_is_directory=True)

    assert exposed_grant(root, [link]) == link


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "project"
    workspace.mkdir()
    return workspace, tmp_path / "runtime" / "vibesys"


def _landlock(workspace: Path, root: Path, grant: Path) -> object:
    return LandlockSandbox(
        workspace=workspace,
        scratch_write_roots=("/dev", "/proc", str(grant)),
        operator_root=root,
    )


def _bubblewrap(workspace: Path, root: Path, grant: Path) -> object:
    return HostSandbox(
        bwrap_path="/usr/bin/bwrap",
        workspace=workspace,
        read_paths=(grant,),
        operator_root=root,
    )


_GRANTING_BACKENDS: list[Callable[[Path, Path, Path], object]] = [_landlock, _bubblewrap]


@pytest.mark.parametrize("build", _GRANTING_BACKENDS, ids=["landlock", "bubblewrap"])
@pytest.mark.parametrize("grant_of", ["ancestor", "root", "inside"])
def test_a_granting_backend_refuses_a_view_that_reaches_the_root(
    tmp_path: Path, build: Callable[[Path, Path, Path], object], grant_of: str
) -> None:
    workspace, root = _layout(tmp_path)
    grant = {"ancestor": root.parent, "root": root, "inside": root / "runs"}[grant_of]

    with pytest.raises(SandboxUnavailableError, match="operator's runtime root"):
        build(workspace, root, grant)


@pytest.mark.parametrize("build", _GRANTING_BACKENDS, ids=["landlock", "bubblewrap"])
def test_a_granting_backend_builds_a_view_beside_the_root(
    tmp_path: Path, build: Callable[[Path, Path, Path], object]
) -> None:
    workspace, root = _layout(tmp_path)
    sibling = tmp_path / "scratch"
    sibling.mkdir()

    build(workspace, root, sibling)


def test_landlock_refuses_the_fallback_root_under_its_tmp_grant(tmp_path: Path) -> None:
    """Without ``XDG_RUNTIME_DIR`` the root is ``/tmp/vibesys-<uid>``, inside the ``/tmp`` grant."""
    workspace, _ = _layout(tmp_path)

    with pytest.raises(SandboxUnavailableError, match="XDG_RUNTIME_DIR"):
        LandlockSandbox(workspace=workspace, operator_root=Path("/tmp/vibesys-1000"))  # noqa: S108  # lint-waiver: LW-313305 [S108]; the fallback runtime root the refusal is about is this fixed literal.


def test_seatbelt_denies_the_root_after_every_allow(tmp_path: Path) -> None:
    workspace, root = _layout(tmp_path)
    profile = SeatbeltSandbox(
        sandbox_exec_path="/usr/bin/sandbox-exec",
        workspace=workspace,
        write_paths=(root.parent,),
        operator_root=root,
    ).profile()

    deny = f'(deny file-read* file-write* (subpath "{root.resolve()}"))'
    lines = profile.splitlines()
    assert deny in lines
    last_allow = max(index for index, line in enumerate(lines) if line.startswith("(allow"))
    assert lines.index(deny) > last_allow


@pytest.mark.parametrize("mount_of", ["ancestor", "root", "inside", "workspace"])
def test_docker_refuses_a_mount_that_reaches_the_root(tmp_path: Path, mount_of: str) -> None:
    workspace, root = _layout(tmp_path)
    source = {
        "ancestor": tmp_path,
        "root": root,
        "inside": root / "runs",
        "workspace": workspace,
    }[mount_of]
    host_workspace = root.parent if mount_of == "workspace" else workspace

    with pytest.raises(SandboxUnavailableError, match="operator's runtime root"):
        DockerSandbox(
            host_workspace=str(host_workspace),
            image="python:3.12-bookworm",
            bind_mounts=[] if mount_of == "workspace" else [(str(source), "/mnt/probe", True)],
            docker=ScriptedDockerCli(),
            threads=SimThreads(),
            operator_root=root,
        )


def test_docker_mounts_beside_the_root(tmp_path: Path) -> None:
    workspace, root = _layout(tmp_path)
    sibling = tmp_path / "logs"

    DockerSandbox(
        host_workspace=str(workspace),
        image="python:3.12-bookworm",
        bind_mounts=[(str(sibling), "/logs", False)],
        docker=ScriptedDockerCli(),
        threads=SimThreads(),
        operator_root=root,
    )


def _granted_link(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A project, the root, and a granted symlink that points beside the root for now."""
    workspace, root = _layout(tmp_path)
    root.mkdir(parents=True)
    beside = tmp_path / "beside"
    beside.mkdir()
    link = tmp_path / "granted"
    link.symlink_to(beside, target_is_directory=True)
    return workspace, root, link


def _retarget(link: Path, target: Path) -> None:
    link.unlink()
    link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("target_of", ["root", "inside", "ancestor"])
def test_bubblewrap_refuses_at_wrap_a_grant_retargeted_into_the_root(
    tmp_path: Path, target_of: str
) -> None:
    """The grant is checked as ``wrap`` binds it, not only as it resolved at construction."""
    workspace, root, link = _granted_link(tmp_path)
    sandbox = HostSandbox(
        bwrap_path="/usr/bin/bwrap", workspace=workspace, read_paths=(link,), operator_root=root
    )
    sandbox.wrap(["true"])

    _retarget(link, {"root": root, "inside": root / "runs", "ancestor": root.parent}[target_of])
    (root / "runs").mkdir(exist_ok=True)

    with pytest.raises(SandboxUnavailableError, match="operator's runtime root"):
        sandbox.wrap(["true"])


@pytest.mark.parametrize("target_of", ["root", "inside", "ancestor"])
def test_docker_refuses_at_start_a_mount_retargeted_into_the_root(
    tmp_path: Path, target_of: str
) -> None:
    """The engine binds a mount as it resolves at ``docker run``, so it is checked there too."""
    workspace, root, link = _granted_link(tmp_path)
    docker = ScriptedDockerCli()
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image="python:3.12-bookworm",
        bind_mounts=[(str(link), "/mnt/probe", True)],
        docker=docker,
        threads=SimThreads(),
        operator_root=root,
    )

    _retarget(link, {"root": root, "inside": root / "runs", "ancestor": root.parent}[target_of])
    (root / "runs").mkdir(exist_ok=True)

    with pytest.raises(SandboxUnavailableError, match="operator's runtime root"):
        sandbox.start(output=DISCARD)
    assert docker.calls == []


@pytest.mark.parametrize("target_of", ["root", "inside", "ancestor"])
def test_landlock_refuses_at_policy_a_grant_retargeted_into_the_root(
    tmp_path: Path, target_of: str
) -> None:
    """The grant is checked as ``policy`` compiles it, so ``wrap`` cannot grant the root."""
    workspace, root, link = _granted_link(tmp_path)
    sandbox = LandlockSandbox(
        workspace=workspace,
        read_paths=(link,),
        scratch_write_roots=("/dev", "/proc"),
        operator_root=root,
    )
    sandbox.policy()

    _retarget(link, {"root": root, "inside": root / "runs", "ancestor": root.parent}[target_of])
    (root / "runs").mkdir(exist_ok=True)

    with pytest.raises(SandboxUnavailableError, match="operator's runtime root"):
        sandbox.policy()
    with pytest.raises(SandboxUnavailableError, match="operator's runtime root"):
        sandbox.wrap(["true"])


def test_only_bubblewrap_and_docker_claim_the_operator_control_boundary() -> None:
    """Landlock and Seatbelt cannot stop a connect to a pathname socket; the type says so."""
    claims = {
        backend.__name__: backend.claims_operator_control_boundary
        for backend in (HostSandbox, DockerSandbox, LandlockSandbox, SeatbeltSandbox)
    }

    assert claims == {
        "HostSandbox": True,
        "DockerSandbox": True,
        "LandlockSandbox": False,
        "SeatbeltSandbox": False,
    }
