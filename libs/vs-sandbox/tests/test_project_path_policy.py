"""Tests for nested project-path confinement policy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable  # noqa: TC003  # tracked: #288
from pathlib import Path

import pytest

import vs_sandbox
from vs_sandbox import host_sandbox
from vs_sandbox.host_resources import HostResource
from vs_sandbox.project_paths import ProjectPathPolicy, ProjectPathPolicyError


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".state" / "local").mkdir(parents=True)
    (workspace / ".state" / "run.json").write_text("{}\n")
    (workspace / ".env").write_text("TOKEN=secret\n")
    (workspace / "Cargo.toml").write_text("[package]\n")
    return workspace


def _policy() -> ProjectPathPolicy:
    return ProjectPathPolicy(
        read_only_paths=(".state", "Cargo.toml"),
        hidden_paths=(".state/local", ".env"),
    )


def _sequence_index(argv: list[str], *parts: str) -> int:
    width = len(parts)
    for index in range(len(argv) - width + 1):
        if argv[index : index + width] == list(parts):
            return index
    pytest.fail(f"sequence not found: {parts!r}")


def _working_bwrap() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return None
    command = [bwrap, "--unshare-user"]
    for root in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(root).exists():
            command += ["--ro-bind", root, root]
    probe = "/usr/bin/true" if Path("/usr/bin/true").exists() else "/bin/true"
    command.append(probe)
    try:
        result = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bwrap if result.returncode == 0 else None


class TestProjectPathPolicy:
    def test_public_export(self) -> None:
        assert vs_sandbox.ProjectPathPolicy is ProjectPathPolicy
        assert vs_sandbox.ProjectPathPolicyError is ProjectPathPolicyError
        assert vs_sandbox.SandboxUnavailableError is host_sandbox.SandboxUnavailableError

    def test_empty_policy_preserves_legacy_nonexistent_workspace_behavior(
        self, tmp_path: Path
    ) -> None:
        ProjectPathPolicy().validate(tmp_path / "missing")

    @pytest.mark.parametrize(
        "path",
        [
            Path("/absolute"),
            Path("../outside"),
            Path("nested/../../outside"),
            Path(),
        ],
    )
    def test_rejects_non_nested_paths(self, path: Path) -> None:
        with pytest.raises(ValueError, match=r"workspace|relative|\.\."):
            ProjectPathPolicy(hidden_paths=(path,))

    @pytest.mark.parametrize(
        ("hidden", "read_only"),
        [
            ((".state", ".state/local"), ()),
            ((), ("src", "src/generated")),
            ((".state",), (".state",)),
            ((".state",), (".state/schema.json",)),
        ],
    )
    def test_rejects_redundant_or_unreachable_overlap(
        self,
        hidden: tuple[str, ...],
        read_only: tuple[str, ...],
    ) -> None:
        with pytest.raises(ValueError, match=r"overlapping|both hidden|nested below hidden"):
            ProjectPathPolicy(hidden_paths=hidden, read_only_paths=read_only)

    def test_allows_hidden_child_of_read_only_parent(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        policy = _policy()

        policy.validate(workspace)

        assert policy.read_only_paths == (Path(".state"), Path("Cargo.toml"))
        assert policy.hidden_paths == (Path(".state/local"), Path(".env"))

    def test_rejects_missing_path(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        policy = ProjectPathPolicy(hidden_paths=("missing",))

        with pytest.raises(ValueError, match="does not exist"):
            policy.validate(workspace)

    def test_rejects_missing_or_non_directory_workspace(self, tmp_path: Path) -> None:
        policy = ProjectPathPolicy(hidden_paths=(".state",))

        with pytest.raises(ValueError, match="workspace does not exist"):
            policy.validate(tmp_path / "missing")

        workspace_file = tmp_path / "workspace-file"
        workspace_file.write_text("not a directory\n")
        with pytest.raises(ValueError, match="workspace is not a directory"):
            policy.validate(workspace_file)

    def test_rejects_symlink_that_escapes_workspace(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace / "escape").symlink_to(outside, target_is_directory=True)
        policy = ProjectPathPolicy(hidden_paths=("escape",))

        with pytest.raises(ValueError, match="resolves outside workspace"):
            policy.validate(workspace)

    def test_rejects_symlink_to_workspace_root(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        (workspace / "self").symlink_to(workspace, target_is_directory=True)

        with pytest.raises(ValueError, match="workspace itself"):
            ProjectPathPolicy(hidden_paths=("self",)).validate(workspace)

    def test_rejects_non_file_or_directory(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        os.mkfifo(workspace / "pipe")

        with pytest.raises(ValueError, match="regular file or directory"):
            ProjectPathPolicy(read_only_paths=("pipe",)).validate(workspace)

    def test_rechecks_overlap_after_resolving_symlinks(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        (workspace / "alias").symlink_to(workspace / ".state", target_is_directory=True)
        policy = ProjectPathPolicy(hidden_paths=("alias/local",), read_only_paths=(".state/local",))

        with pytest.raises(ValueError, match="both hidden and read-only"):
            policy.validate(workspace)


class TestBubblewrapProjectPaths:
    def test_overlays_follow_workspace_bind_and_hidden_masks_come_last(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = _workspace(tmp_path)
        sandbox = host_sandbox.HostSandbox(
            workspace=workspace,
            bwrap_path="/usr/bin/bwrap",
            system_read_roots=(),
            project_path_policy=_policy(),
        )

        argv = sandbox.wrap(["agent"])

        workspace_bind = _sequence_index(argv, "--bind", str(workspace), str(workspace))
        readonly_dir = _sequence_index(
            argv,
            "--ro-bind",
            str(workspace / ".state"),
            str(workspace / ".state"),
        )
        readonly_file = _sequence_index(
            argv,
            "--ro-bind",
            str(workspace / "Cargo.toml"),
            str(workspace / "Cargo.toml"),
        )
        hidden_dir = _sequence_index(argv, "--tmpfs", str(workspace / ".state" / "local"))
        hidden_file = _sequence_index(argv, "--ro-bind", "/dev/null", str(workspace / ".env"))
        chdir = _sequence_index(argv, "--chdir", str(workspace))

        assert workspace_bind < readonly_dir < hidden_dir < chdir
        assert workspace_bind < readonly_file < hidden_file < chdir
        assert max(readonly_dir, readonly_file) < min(hidden_dir, hidden_file)

    @pytest.mark.skipif(_working_bwrap() is None, reason="requires working bubblewrap")
    def test_enforces_file_and_directory_policy(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        bwrap = _working_bwrap()
        assert bwrap is not None
        sandbox = host_sandbox.HostSandbox(
            workspace=workspace,
            bwrap_path=bwrap,
            project_path_policy=_policy(),
        )
        script = (
            "test ! -e .state/local/host-secret"
            " && test ! -s .env"
            " && ! printf changed > .env"
            " && ! printf changed > Cargo.toml"
            " && ! printf changed > .state/run.json"
            " && printf ephemeral > .state/local/agent-file"
            " && printf editable > source.txt"
        )
        (workspace / ".state" / "local" / "host-secret").write_text("secret\n")

        result = subprocess.run(  # noqa: S603
            sandbox.wrap(["/bin/sh", "-c", script]),
            capture_output=True,
            check=False,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert (workspace / ".env").read_text() == "TOKEN=secret\n"
        assert (workspace / "Cargo.toml").read_text() == "[package]\n"
        assert (workspace / ".state" / "run.json").read_text() == "{}\n"
        assert not (workspace / ".state" / "local" / "agent-file").exists()
        assert (workspace / "source.txt").read_text() == "editable"

    def test_builder_passes_validated_policy_to_linux_backend(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        policy = _policy()
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(
            host_sandbox.shutil,
            "which",
            lambda *_args, **_kwargs: "/usr/bin/bwrap",
        )
        # The builder probes that bwrap can actually unshare a user namespace;
        # this test supplies a path rather than a working binary.
        monkeypatch.setattr(host_sandbox, "_bwrap_confines", lambda _path: True)

        sandbox = host_sandbox.build(
            workspace,
            env={},
            project_path_policy=policy,
            require_enforcement=True,
        )

        assert isinstance(sandbox, host_sandbox.HostSandbox)
        assert sandbox.project_path_policy is policy


class TestSeatbeltProjectPaths:
    def test_specific_denies_follow_workspace_allows_for_files_and_directories(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = _workspace(tmp_path)
        sandbox = host_sandbox.SeatbeltSandbox(
            workspace=workspace,
            sandbox_exec_path="/usr/bin/sandbox-exec",
            project_path_policy=_policy(),
        )

        profile = sandbox.profile()
        workspace_allow = f'(allow file-read* file-write* (subpath "{workspace}"))'
        readonly_dir = f'(deny file-write* (subpath "{workspace / ".state"}"))'
        readonly_file = f'(deny file-write* (literal "{workspace / "Cargo.toml"}"))'
        hidden_dir = f'(deny file-read* file-write* (subpath "{workspace / ".state" / "local"}"))'
        hidden_file = f'(deny file-read* file-write* (literal "{workspace / ".env"}"))'

        allow_index = profile.index(workspace_allow)
        readonly_indexes = (profile.index(readonly_dir), profile.index(readonly_file))
        hidden_indexes = (profile.index(hidden_dir), profile.index(hidden_file))
        assert allow_index < min(readonly_indexes)
        assert max(readonly_indexes) < min(hidden_indexes)


class TestRequiredEnforcement:
    @pytest.mark.parametrize(
        ("platform", "tool"),
        [("linux", "bwrap"), ("darwin", "sandbox-exec"), ("win32", "backend")],
    )
    def test_unavailable_backend_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        platform: str,
        tool: str,
    ) -> None:
        monkeypatch.setattr(host_sandbox.sys, "platform", platform)
        monkeypatch.setattr(host_sandbox.shutil, "which", lambda *_args, **_kwargs: None)

        with pytest.raises(host_sandbox.SandboxUnavailableError, match=tool):
            host_sandbox.build(tmp_path, env={}, require_enforcement=True)

    def test_operator_disable_fails_when_enforcement_is_required(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(host_sandbox.SandboxUnavailableError, match="DISABLED"):
            host_sandbox.build(
                tmp_path,
                env={host_sandbox.DISABLE_ENV: "off"},
                require_enforcement=True,
            )

    def test_default_still_returns_none_when_backend_is_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(host_sandbox.shutil, "which", lambda *_args, **_kwargs: None)
        logs: list[str] = []

        result = host_sandbox.build(tmp_path, env={}, log=logs.append)

        assert result is None
        assert any("bwrap" in message for message in logs)


class TestHostResourceAgentPathRejection:
    """Host backends run on the host filesystem, so none of them can remap.

    Only a container backend may present a resource at a different path than
    its host location; ``build()`` fails fast for the three host backends
    rather than silently ignoring an ``agent_path`` it cannot honor.
    """

    def test_matching_agent_path_is_accepted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        resource_path = tmp_path / "toolchain"
        resource_path.mkdir()
        resource = HostResource(resource_path, agent_path=str(resource_path))
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(
            host_sandbox.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/bwrap"
        )
        monkeypatch.setattr(host_sandbox, "_bwrap_confines", lambda _path: True)

        sandbox = host_sandbox.build(
            workspace,
            env={},
            resources=(resource,),
            require_enforcement=True,
        )

        assert isinstance(sandbox, host_sandbox.HostSandbox)

    def test_unset_agent_path_is_accepted(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        resource = HostResource(tmp_path / "toolchain")

        # No backend needs to be available: rejection happens before dispatch.
        host_sandbox.build(workspace, env={}, resources=(resource,))

    def test_mismatched_agent_path_names_the_resource_and_the_backends(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = _workspace(tmp_path)
        resource_path = tmp_path / "toolchain"
        resource = HostResource(
            resource_path,
            purpose="test toolchain",
            agent_path="/opt/vibesys-toolchain",
        )

        with pytest.raises(ValueError, match="test toolchain") as excinfo:
            host_sandbox.build(workspace, env={}, resources=(resource,))

        message = str(excinfo.value)
        assert str(resource_path) in message
        assert "/opt/vibesys-toolchain" in message
        assert "bubblewrap" in message
        assert "Landlock" in message
        assert "Seatbelt" in message

    def test_rejection_survives_a_one_shot_resource_iterable(self, tmp_path: Path) -> None:
        """A generator of resources must not be silently exhausted before validation."""
        workspace = _workspace(tmp_path)
        resource = HostResource(
            tmp_path / "toolchain",
            purpose="generator resource",
            agent_path="/opt/vibesys-toolchain",
        )

        def resources() -> Iterable[HostResource]:
            yield resource

        with pytest.raises(ValueError, match="generator resource"):
            host_sandbox.build(workspace, env={}, resources=resources())


class TestWorkspaceSandboxAgentPath:
    def test_host_backends_return_the_host_path_normalised(self, tmp_path: Path) -> None:
        sandbox = host_sandbox.HostSandbox(
            workspace=_workspace(tmp_path),
            bwrap_path="/usr/bin/bwrap",
        )

        assert sandbox.agent_path("/foo//bar/") == "/foo/bar"
        assert sandbox.agent_path(Path("/foo/./bar")) == "/foo/bar"


class TestWorkspaceSandboxEnv:
    def test_falls_back_to_os_environ_without_a_build_env(self, tmp_path: Path) -> None:
        sandbox = host_sandbox.HostSandbox(
            workspace=_workspace(tmp_path),
            bwrap_path="/usr/bin/bwrap",
        )

        assert sandbox.env is os.environ

    def test_returns_the_build_env_unmodified_when_it_has_home_and_path(
        self, tmp_path: Path
    ) -> None:
        build_env = {"HOME": "/home/agent", "PATH": "/usr/bin", "EXTRA": "1"}
        sandbox = host_sandbox.HostSandbox(
            workspace=_workspace(tmp_path),
            bwrap_path="/usr/bin/bwrap",
            build_env=build_env,
        )

        assert sandbox.env == build_env
        assert sandbox.env is build_env

    def test_fills_in_a_missing_home_or_path_from_os_environ(self, tmp_path: Path) -> None:
        build_env = {"PATH": "/usr/bin"}
        sandbox = host_sandbox.HostSandbox(
            workspace=_workspace(tmp_path),
            bwrap_path="/usr/bin/bwrap",
            build_env=build_env,
        )

        env = sandbox.env

        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == os.environ["HOME"]

    def test_build_passes_the_caller_env_through_to_the_sandbox(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(
            host_sandbox.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/bwrap"
        )
        monkeypatch.setattr(host_sandbox, "_bwrap_confines", lambda _path: True)
        env = {"HOME": "/home/agent", "PATH": "/usr/bin"}

        sandbox = host_sandbox.build(workspace, env=env, require_enforcement=True)

        assert isinstance(sandbox, host_sandbox.HostSandbox)
        assert sandbox.env == env


class TestBuildSelectsDocker:
    """``docker=`` short-circuits ``build()`` past every host backend."""

    def test_docker_sandbox_is_returned_unchanged(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        # Any WorkspaceSandbox stands in for a real DockerSandbox here: the
        # point under test is that build() returns it untouched, not that it
        # is specifically a Docker one.
        docker_sandbox = host_sandbox.HostSandbox(workspace=workspace, bwrap_path="/usr/bin/bwrap")

        result = host_sandbox.build(workspace, env={}, docker=docker_sandbox)

        assert result is docker_sandbox

    def test_docker_short_circuits_before_resource_validation(self, tmp_path: Path) -> None:
        """A mismatched ``agent_path`` would normally raise before dispatch;
        selecting Docker skips that host-only check entirely."""
        workspace = _workspace(tmp_path)
        resource = HostResource(
            tmp_path / "toolchain",
            purpose="test toolchain",
            agent_path="/opt/vibesys-toolchain",
        )
        # Any WorkspaceSandbox stands in for a real DockerSandbox here: the
        # point under test is that build() returns it untouched, not that it
        # is specifically a Docker one.
        docker_sandbox = host_sandbox.HostSandbox(workspace=workspace, bwrap_path="/usr/bin/bwrap")

        result = host_sandbox.build(workspace, env={}, resources=(resource,), docker=docker_sandbox)

        assert result is docker_sandbox

    def test_host_dispatch_runs_unchanged_when_docker_is_not_given(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(
            host_sandbox.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/bwrap"
        )
        monkeypatch.setattr(host_sandbox, "_bwrap_confines", lambda _path: True)

        sandbox = host_sandbox.build(workspace, env={}, require_enforcement=True)

        assert isinstance(sandbox, host_sandbox.HostSandbox)
