"""Tests for the Landlock host-confinement backend.

The behavioral tests execute a real shell under a real ruleset, because the
only thing worth asserting about a sandbox is what the kernel actually denies.
They are split along the boundary the backend documents: the outer project
boundary is enforced, and the nested :class:`ProjectPathPolicy` tiers are not.
That second group is a *characterization* suite. It pins the known weakness so
the gap stays visible and a future kernel or backend change that closes it is
caught rather than silently assumed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import vs_sandbox
from vs_sandbox import host_sandbox, landlock
from vs_sandbox.host_sandbox import LandlockSandbox, LinuxBackend
from vs_sandbox.project_paths import ProjectPathPolicy

requires_landlock = pytest.mark.skipif(
    landlock.abi_version() is None,
    reason="requires a kernel with Landlock support",
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".state" / "local").mkdir(parents=True)
    (workspace / ".state" / "run.json").write_text("{}\n")
    (workspace / ".env").write_text("TOKEN=secret\n")
    (workspace / "Cargo.toml").write_text("[package]\n")
    (workspace / "source.txt").write_text("original")
    return workspace


def _policy() -> ProjectPathPolicy:
    return ProjectPathPolicy(
        read_only_paths=(".state", "Cargo.toml"),
        hidden_paths=(".state/local", ".env"),
    )


def _confined(workspace: Path, **kwargs: object) -> LandlockSandbox:
    """Build a sandbox whose granted scratch roots exclude the fixture's tree.

    ``tmp_path`` lives under ``/tmp``, which the production scratch roots grant
    write access to, and a granted ancestor cannot be narrowed. Production
    refuses that layout outright (see :class:`TestLandlockRefusesUnconfinable`);
    behavioral tests instead drop ``/tmp`` so the boundary under test is the
    project's own.
    """
    return LandlockSandbox(
        workspace=workspace,
        scratch_write_roots=("/dev", "/proc"),
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


def _run(sandbox: LandlockSandbox, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        sandbox.wrap(["/bin/sh", "-c", script]),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


class TestLandlockPolicyCompilation:
    def test_public_export(self) -> None:
        assert vs_sandbox.LandlockSandbox is LandlockSandbox
        assert vs_sandbox.LinuxBackend is LinuxBackend

    def test_workspace_is_writable_and_system_roots_are_read_only(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sandbox = _confined(workspace, system_read_roots=("/usr",))

        rules = {rule.path: rule.access for rule in sandbox.policy().rules}

        assert rules[str(workspace.resolve())] is landlock.RuleAccess.FULL
        assert rules["/usr"] is landlock.RuleAccess.READ

    def test_wrap_reexecs_through_the_landlock_entry_point(self, tmp_path: Path) -> None:
        sandbox = _confined(_workspace(tmp_path))

        argv = sandbox.wrap(["agent", "--flag"])

        assert argv[:3] == [sys.executable, "-m", "vs_sandbox.landlock"]
        assert argv[-3:] == ["--", "agent", "--flag"]

    def test_unenforced_policy_names_every_tier_it_drops(self, tmp_path: Path) -> None:
        sandbox = LandlockSandbox(workspace=_workspace(tmp_path), project_path_policy=_policy())

        gaps = "\n".join(sandbox.unenforced_policy())

        assert ".state" in gaps
        assert "Cargo.toml" in gaps
        assert ".env" in gaps

    def test_unenforced_policy_is_empty_without_a_nested_policy(self, tmp_path: Path) -> None:
        sandbox = LandlockSandbox(workspace=_workspace(tmp_path))

        assert sandbox.unenforced_policy() == ()


@requires_landlock
class TestLandlockEnforcesTheProjectBoundary:
    """The guarantee this backend does make, and the escape from issue #149."""

    def test_project_is_writable(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sandbox = _confined(workspace)

        result = _run(sandbox, "printf edited > source.txt && printf new > created.txt")

        assert result.returncode == 0, result.stderr
        assert (workspace / "source.txt").read_text() == "edited"
        assert (workspace / "created.txt").read_text() == "new"

    def test_paths_outside_the_project_are_denied(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("sibling secret\n")
        sandbox = _confined(workspace)

        result = _run(
            sandbox,
            f"cat {sibling / 'secret.txt'} 2>/dev/null; echo read=$?;"
            f" (printf x > {sibling / 'escape.txt'}) 2>/dev/null; echo write=$?",
        )

        assert "read=1" in result.stdout
        assert "write=" in result.stdout
        assert "write=0" not in result.stdout
        assert not (sibling / "escape.txt").exists()

    def test_declared_read_resources_stay_readable_but_not_writable(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        resource = tmp_path / "toolchain"
        resource.mkdir()
        (resource / "config.json").write_text("{}\n")
        sandbox = _confined(workspace, read_paths=(resource,))

        result = _run(
            sandbox,
            f"cat {resource / 'config.json'} >/dev/null 2>&1; echo read=$?;"
            f" (printf x > {resource / 'config.json'}) 2>/dev/null; echo write=$?",
        )

        assert "read=0" in result.stdout
        assert "write=0" not in result.stdout
        assert (resource / "config.json").read_text() == "{}\n"


@pytest.mark.skipif(
    not landlock.supports_scoping(),
    reason="requires Landlock ABI 6 for scoping",
)
class TestLandlockScopingClosesNonFilesystemChannels:
    """Signals and abstract sockets bypass path rules, so ABI 6 scopes them.

    Without scoping a confined agent can signal, or open an abstract socket to,
    an *unconfined* process owned by the same user and have it act on the
    agent's behalf. Bubblewrap closes the signal route with ``--unshare-pid``
    and leaves abstract sockets open; scoping closes both.
    """

    def test_cannot_signal_a_process_outside_the_sandbox(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sandbox = _confined(workspace)

        # The test runner itself is unconfined and owned by the same uid.
        result = _run(sandbox, f"kill -0 {os.getpid()}")

        assert result.returncode != 0

    def test_in_scope_abstract_sockets_still_work(self, tmp_path: Path) -> None:
        """Scoping must not break the agent's own IPC, only cross-scope IPC."""
        interpreter = Path("/usr/bin/python3")
        if not interpreter.exists():
            pytest.skip("needs an interpreter inside a granted system read root")
        workspace = _workspace(tmp_path)
        sandbox = _confined(workspace)
        program = (
            "import socket;"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);"
            "s.bind('\\0vs-scope-test');s.listen(1);"
            "c=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);"
            "c.connect('\\0vs-scope-test');"
            "print('connected')"
        )

        result = _run(sandbox, f'{interpreter} -c "{program}"')

        assert result.returncode == 0, result.stderr
        assert "connected" in result.stdout


@requires_landlock
class TestLandlockCannotEnforceNestedPolicy:
    """Characterization of the documented gap: rules add rights, never remove.

    If one of these starts failing, Landlock gained the ability to subtract
    within a hierarchy. That is good news, and it means
    :class:`LandlockSandbox` should stop advertising the tier as unenforced.
    """

    def test_read_only_paths_remain_writable(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sandbox = _confined(workspace, project_path_policy=_policy())

        result = _run(sandbox, "printf changed > Cargo.toml")

        assert result.returncode == 0, result.stderr
        assert (workspace / "Cargo.toml").read_text() == "changed"

    def test_hidden_paths_remain_readable(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sandbox = _confined(workspace, project_path_policy=_policy())

        result = _run(sandbox, "cat .env")

        assert result.returncode == 0, result.stderr
        assert "TOKEN=secret" in result.stdout


@requires_landlock
class TestLandlockHelperFailsClosed:
    def test_unparsable_policy_does_not_launch_the_command(self, tmp_path: Path) -> None:
        marker = tmp_path / "ran.txt"

        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "vs_sandbox.landlock",
                "--policy",
                '{"rules": [{"path": "/usr", "access": "bogus-tier"}]}',
                "--",
                "/bin/sh",
                "-c",
                f"printf ran > {marker}",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert not marker.exists()

    def test_missing_command_is_rejected(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "vs_sandbox.landlock", "--policy", "{}", "--"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

        assert result.returncode == 2


class TestLandlockRefusesUnconfinable:
    """A granted ancestor cannot be narrowed, so such a layout must not run."""

    def test_project_inside_a_granted_scratch_root_is_refused(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sandbox = LandlockSandbox(
            workspace=workspace,
            scratch_write_roots=(str(tmp_path),),
        )

        with pytest.raises(host_sandbox.SandboxUnavailableError, match="unconfined"):
            sandbox.policy()

    def test_project_inside_a_declared_write_resource_is_refused(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sandbox = LandlockSandbox(
            workspace=workspace,
            scratch_write_roots=(),
            write_paths=(tmp_path,),
        )

        with pytest.raises(host_sandbox.SandboxUnavailableError, match="unconfined"):
            sandbox.policy()

    def test_sibling_scratch_root_is_allowed(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        sibling = tmp_path / "scratch"
        sibling.mkdir()
        sandbox = LandlockSandbox(workspace=workspace, scratch_write_roots=(str(sibling),))

        assert sandbox.policy().rules


class TestLinuxBackendSelection:
    def test_landlock_is_never_selected_automatically(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default behavior is unchanged: no bwrap means fail closed, not downgrade."""
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(host_sandbox.shutil, "which", lambda *_args, **_kwargs: None)

        with pytest.raises(host_sandbox.SandboxUnavailableError, match="bwrap"):
            host_sandbox.build(workspace, env={}, require_enforcement=True)

    def test_missing_bwrap_message_points_at_the_opt_in(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(host_sandbox.shutil, "which", lambda *_args, **_kwargs: None)
        logs: list[str] = []

        host_sandbox.build(workspace, env={}, log=logs.append)

        assert any("VIBESYS_AGENT_SANDBOX=landlock" in message for message in logs)

    @requires_landlock
    def test_opt_in_selects_landlock_without_bwrap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(host_sandbox.shutil, "which", lambda *_args, **_kwargs: None)
        # The fixture project lives under /tmp, which the production scratch
        # roots would grant and the ancestor guard would then reject.
        monkeypatch.setattr(host_sandbox, "_LINUX_SCRATCH_WRITE_ROOTS", ("/dev", "/proc"))
        logs: list[str] = []

        sandbox = host_sandbox.build(
            workspace,
            env={host_sandbox.DISABLE_ENV: LinuxBackend.LANDLOCK.value},
            project_path_policy=_policy(),
            log=logs.append,
            require_enforcement=True,
        )

        assert isinstance(sandbox, LandlockSandbox)
        assert any("NOT ENFORCED" in message for message in logs)

    def test_unusable_bwrap_is_treated_as_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A blocked bwrap must fail at startup, not once per agent turn."""
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(
            host_sandbox.shutil,
            "which",
            lambda *_args, **_kwargs: "/opt/unpacked/bwrap",
        )
        monkeypatch.setattr(host_sandbox, "_bwrap_confines", lambda _path: False)

        with pytest.raises(host_sandbox.SandboxUnavailableError, match="user namespace"):
            host_sandbox.build(workspace, env={}, require_enforcement=True)

    def test_unknown_backend_value_is_rejected(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)

        with pytest.raises(host_sandbox.SandboxUnavailableError, match="unknown"):
            host_sandbox.build(
                workspace,
                env={host_sandbox.DISABLE_ENV: "landlok"},
                require_enforcement=True,
            )
