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

import ctypes
import errno
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

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


def _confined(
    workspace: Path,
    *,
    read_paths: tuple[Path, ...] = (),
    project_path_policy: ProjectPathPolicy | None = None,
    system_read_roots: tuple[str, ...] = LandlockSandbox.system_read_roots,
) -> LandlockSandbox:
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
        read_paths=read_paths,
        project_path_policy=(
            ProjectPathPolicy() if project_path_policy is None else project_path_policy
        ),
        system_read_roots=system_read_roots,
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

    def test_unknown_backend_value_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")

        with pytest.raises(host_sandbox.SandboxUnavailableError, match="unknown"):
            host_sandbox.build(
                workspace,
                env={host_sandbox.DISABLE_ENV: "landlok"},
                require_enforcement=True,
            )


class _FakeLibc:
    """Stand-in for libc that records the Landlock syscalls ``restrict`` makes.

    The kernel-backed suites above prove what the ruleset denies; this fake
    exists to drive the error paths of the syscall layer, which a working
    kernel never takes, and to inspect the structs handed to the kernel.
    """

    def __init__(
        self,
        *,
        create_result: int | None = None,
        add_rule_result: int = 0,
        prctl_result: int = 0,
        restrict_result: int = 0,
    ) -> None:
        self.create_result = create_result
        self.add_rule_result = add_rule_result
        self.prctl_result = prctl_result
        self.restrict_result = restrict_result
        self.ruleset_attr: ctypes.Structure | None = None
        self.ruleset_size: int | None = None
        self.rules: list[tuple[int, int]] = []
        self.restricted = False

    def prctl(self, *_args: object) -> int:
        return self.prctl_result

    def syscall(self, number: ctypes.c_long, *args: Any) -> int:  # noqa: ANN401
        create, add_rule, restrict_self = landlock._syscall_numbers()  # noqa: SLF001
        if number.value == create:
            if self.create_result is not None:
                return self.create_result
            self.ruleset_attr = args[0]._obj  # noqa: SLF001
            self.ruleset_size = args[1].value
            return os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
        if number.value == add_rule:
            attr = args[2]._obj  # noqa: SLF001
            self.rules.append((int(attr.allowed_access), int(attr.parent_fd)))
            return self.add_rule_result
        assert number.value == restrict_self
        self.restricted = self.restrict_result == 0
        return self.restrict_result


@pytest.fixture
def fake_libc(monkeypatch: pytest.MonkeyPatch) -> _FakeLibc:
    libc = _FakeLibc()
    monkeypatch.setattr(landlock, "_libc", lambda: libc)
    monkeypatch.setattr(landlock, "_syscall_numbers", lambda: (444, 445, 446))
    monkeypatch.setattr(landlock, "abi_version", lambda: landlock._ABI_SCOPED)  # noqa: SLF001
    return libc


class TestLandlockAbiProbe:
    def test_unknown_architecture_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(landlock.platform, "machine", lambda: "mips")

        with pytest.raises(landlock.LandlockUnavailableError, match="mips"):
            landlock._syscall_numbers()  # noqa: SLF001
        assert landlock.abi_version() is None

    def test_non_linux_has_no_abi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(landlock.sys, "platform", "darwin")

        assert landlock.abi_version() is None

    def test_syscall_failure_means_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(landlock, "_libc", lambda: _FakeLibc(create_result=-1))
        monkeypatch.setattr(landlock, "_syscall_numbers", lambda: (444, 445, 446))

        assert landlock.abi_version() is None

    def test_scoping_needs_abi_six(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(landlock, "abi_version", lambda: 5)
        assert landlock.supports_scoping() is False
        monkeypatch.setattr(landlock, "abi_version", lambda: 6)
        assert landlock.supports_scoping() is True
        monkeypatch.setattr(landlock, "abi_version", lambda: None)
        assert landlock.supports_scoping() is False

    @pytest.mark.parametrize(
        ("abi", "dropped"),
        [
            (1, landlock.AccessFS.TRUNCATE | landlock.AccessFS.REFER),
            (2, landlock.AccessFS.TRUNCATE),
            (3, landlock.AccessFS(0)),
        ],
    )
    def test_handled_access_drops_rights_older_kernels_reject(
        self,
        abi: int,
        dropped: landlock.AccessFS,
    ) -> None:
        handled = landlock._handled_access(abi)  # noqa: SLF001

        assert handled & dropped == 0
        assert handled | dropped == landlock._ACCESS_BITS[landlock.RuleAccess.FULL]  # noqa: SLF001


class TestLandlockRestrictSyscalls:
    def test_applies_every_rule_then_restricts(self, tmp_path: Path, fake_libc: _FakeLibc) -> None:
        (tmp_path / "file.txt").write_text("x")
        policy = landlock.policy_for(
            read_paths=(tmp_path / "file.txt",),
            write_paths=(tmp_path,),
        )

        abi = landlock.restrict(policy)

        assert abi == landlock._ABI_SCOPED  # noqa: SLF001
        assert fake_libc.restricted
        full = int(landlock._ACCESS_BITS[landlock.RuleAccess.READ])  # noqa: SLF001
        assert [access for access, _fd in fake_libc.rules] == [
            full & int(landlock._FILE_BITS),  # noqa: SLF001
            int(landlock._ACCESS_BITS[landlock.RuleAccess.FULL]),  # noqa: SLF001
        ]

    def test_abi_six_scopes_signals_and_abstract_sockets(self, fake_libc: _FakeLibc) -> None:
        landlock.restrict(landlock.LandlockPolicy())

        attr = fake_libc.ruleset_attr
        assert isinstance(attr, landlock._ScopedRulesetAttr)  # noqa: SLF001
        assert attr.scoped == (
            landlock._LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET | landlock._LANDLOCK_SCOPE_SIGNAL  # noqa: SLF001
        )
        assert attr.handled_access_net == 0
        assert fake_libc.ruleset_size == ctypes.sizeof(attr)

    def test_older_abi_uses_filesystem_only_struct(
        self,
        fake_libc: _FakeLibc,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(landlock, "abi_version", lambda: 1)

        landlock.restrict(landlock.LandlockPolicy())

        attr = fake_libc.ruleset_attr
        assert isinstance(attr, landlock._RulesetAttr)  # noqa: SLF001
        assert attr.handled_access_fs & landlock.AccessFS.TRUNCATE == 0
        assert fake_libc.ruleset_size == ctypes.sizeof(ctypes.c_uint64)

    def test_missing_rule_paths_are_skipped(self, tmp_path: Path, fake_libc: _FakeLibc) -> None:
        policy = landlock.policy_for(read_paths=(tmp_path / "absent",), write_paths=())

        landlock.restrict(policy)

        assert fake_libc.rules == []
        assert fake_libc.restricted

    def test_unsupported_kernel_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(landlock, "abi_version", lambda: None)

        with pytest.raises(landlock.LandlockUnavailableError, match="does not support"):
            landlock.restrict(landlock.LandlockPolicy())

    def test_create_ruleset_failure_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(landlock, "_libc", lambda: _FakeLibc(create_result=-1))
        monkeypatch.setattr(landlock, "_syscall_numbers", lambda: (444, 445, 446))
        monkeypatch.setattr(landlock, "abi_version", lambda: 1)

        with pytest.raises(landlock.LandlockUnavailableError, match="create_ruleset"):
            landlock.restrict(landlock.LandlockPolicy())

    @pytest.mark.parametrize(
        ("failure", "message"),
        [
            ({"add_rule_result": -1}, "add_rule"),
            ({"prctl_result": -1}, "NO_NEW_PRIVS"),
            ({"restrict_result": -1}, "restrict_self"),
        ],
    )
    def test_later_syscall_failures_are_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: dict[str, int],
        message: str,
    ) -> None:
        libc = _FakeLibc(**failure)
        monkeypatch.setattr(landlock, "_libc", lambda: libc)
        monkeypatch.setattr(landlock, "_syscall_numbers", lambda: (444, 445, 446))
        monkeypatch.setattr(landlock, "abi_version", lambda: 1)
        policy = landlock.policy_for(read_paths=(), write_paths=(tmp_path,))

        with pytest.raises(landlock.LandlockUnavailableError, match=message):
            landlock.restrict(policy)
        assert not libc.restricted


class TestLandlockEntryPoint:
    """``main()`` driven in-process, so its branches are measurable."""

    def test_usage_without_separator(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert landlock.main(["--policy", "{}"]) == 2
        assert "usage:" in capsys.readouterr().err

    def test_missing_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert landlock.main(["--policy", "{}", "--"]) == 2
        assert "no command" in capsys.readouterr().err

    def test_restricts_then_execs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        applied: list[landlock.LandlockPolicy] = []
        execs: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(landlock, "restrict", lambda policy: applied.append(policy) or 1)
        monkeypatch.setattr(landlock.os, "execvp", lambda file, args: execs.append((file, args)))
        policy = landlock.policy_for(read_paths=(), write_paths=(tmp_path,), chdir=tmp_path)

        result = landlock.main(["--policy", policy.model_dump_json(), "--", "agent", "--flag"])

        assert result is None
        assert applied == [policy]
        assert execs == [("agent", ["agent", "--flag"])]
        assert Path.cwd() == tmp_path.resolve()

    def test_unavailable_landlock_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def refuse(_policy: landlock.LandlockPolicy) -> int:
            raise landlock.LandlockUnavailableError("nope")

        monkeypatch.setattr(landlock, "restrict", refuse)
        monkeypatch.setattr(landlock.os, "execvp", lambda *_args: pytest.fail("must not exec"))

        assert landlock.main(["--policy", "{}", "--", "agent"]) == 125
        assert "refusing to launch unconfined" in capsys.readouterr().err

    def test_unexecutable_command(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(landlock, "restrict", lambda _policy: 1)

        assert landlock.main(["--policy", "{}", "--", "/nonexistent/agent"]) == 127
        assert "cannot execute" in capsys.readouterr().err

    def test_module_entry_point_exits_with_main_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["landlock", "--policy", "{}"])

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path(landlock.__file__, run_name="__main__")

        assert excinfo.value.code == 2


class TestLinuxBackendProbes:
    def test_bwrap_that_cannot_launch_does_not_confine(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.ENOEXEC, "Exec format error")

        monkeypatch.setattr(host_sandbox.subprocess, "run", explode)

        assert host_sandbox._bwrap_confines("/opt/bwrap") is False  # noqa: SLF001

    def test_landlock_opt_in_without_kernel_support_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = _workspace(tmp_path)
        monkeypatch.setattr(host_sandbox.sys, "platform", "linux")
        monkeypatch.setattr(host_sandbox.landlock, "abi_version", lambda: None)

        with pytest.raises(host_sandbox.SandboxUnavailableError, match="does not support Landlock"):
            host_sandbox.build(
                workspace,
                env={host_sandbox.DISABLE_ENV: LinuxBackend.LANDLOCK.value},
                require_enforcement=True,
            )
