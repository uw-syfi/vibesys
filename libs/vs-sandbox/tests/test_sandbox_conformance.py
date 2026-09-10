"""Conformance suite proving every host confinement backend enforces the same
resource-list contract.

Each backend is built from an identical, tiny resource list (a writable
workspace, one declared read-only file, one path nothing declares) and probed
through :meth:`~vs_sandbox.host_sandbox.WorkspaceSandbox.wrap` with a real
subprocess. bubblewrap and Landlock run for real on Linux hosts with the
respective tooling; Seatbelt only runs on macOS. A backend absent from the
current host skips with a reason rather than failing.

This folds in what ``TestLandlockEnforcesTheProjectBoundary`` in
``test_landlock_sandbox.py`` used to check on its own (project writable,
siblings denied, a declared read-only resource stays read-only): that
coverage now lives here, parametrized across every backend instead of pinned
to one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from vs_sandbox import host_sandbox, landlock
from vs_sandbox.host_resources import HostResource, HostResourceAccess
from vs_sandbox.host_sandbox import HostSandbox, LandlockSandbox, LinuxBackend, SeatbeltSandbox

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.host_sandbox import WorkspaceSandbox

# Deliberately distinct from the test process's own HOME/PATH, so a probe
# that echoes the wrong value (falling back to os.environ instead of
# build_env, say) is caught rather than accidentally matching by coincidence.
_PROBE_ENV = {"HOME": "/home/conformance-probe", "PATH": "/usr/bin:/bin"}


def _bwrap_path() -> str | None:
    """Return a working bubblewrap binary, or ``None`` if none is usable."""
    bwrap = shutil.which("bwrap")
    if bwrap is None or not host_sandbox._bwrap_confines(bwrap):  # noqa: SLF001
        return None
    return bwrap


def _build_bubblewrap(
    workspace: Path,
    resources: tuple[HostResource, ...],
    _monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceSandbox:
    if _bwrap_path() is None:
        pytest.skip("requires a working bubblewrap")
    sandbox = host_sandbox.build(
        workspace,
        env=dict(_PROBE_ENV),
        resources=resources,
        require_enforcement=True,
    )
    assert isinstance(sandbox, HostSandbox)
    return sandbox


def _build_landlock(
    workspace: Path,
    resources: tuple[HostResource, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceSandbox:
    if landlock.abi_version() is None:
        pytest.skip("requires a kernel with Landlock support")
    # The fixture workspace lives under /tmp, which the production scratch
    # roots grant write access to, and a granted ancestor cannot be narrowed
    # back down (Landlock rules only add rights). Drop /tmp and /var/tmp here
    # so the boundary under test is the project's own, exactly as
    # test_opt_in_selects_landlock_without_bwrap does.
    monkeypatch.setattr(host_sandbox, "_LINUX_SCRATCH_WRITE_ROOTS", ("/dev", "/proc"))
    sandbox = host_sandbox.build(
        workspace,
        env={**_PROBE_ENV, host_sandbox.DISABLE_ENV: LinuxBackend.LANDLOCK.value},
        resources=resources,
        require_enforcement=True,
    )
    assert isinstance(sandbox, LandlockSandbox)
    return sandbox


def _build_seatbelt(
    workspace: Path,
    resources: tuple[HostResource, ...],
    _monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceSandbox:
    if sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
        pytest.skip("requires macOS with sandbox-exec")
    sandbox = host_sandbox.build(
        workspace,
        env=dict(_PROBE_ENV),
        resources=resources,
        require_enforcement=True,
    )
    assert isinstance(sandbox, SeatbeltSandbox)
    return sandbox


_BACKEND_BUILDERS = {
    "bubblewrap": _build_bubblewrap,
    "landlock": _build_landlock,
    "seatbelt": _build_seatbelt,
}


def _probe_script(workspace: Path, readonly_path: Path, unlisted_path: Path) -> str:
    """Shell script exercising every conformance check in one subprocess."""
    written = workspace / "written-by-probe.txt"
    return (
        f"printf ok > {written}; printf 'write_ws=%s\\n' $?; "
        f"(printf x > {readonly_path}) 2>/dev/null; printf 'write_ro=%s\\n' $?; "
        f"cat {unlisted_path} >/dev/null 2>&1; printf 'read_unlisted=%s\\n' $?; "
        "printf 'uid=%s\\n' \"$(id -u)\"; "
        "printf 'HOME=%s\\n' \"$HOME\"; "
        "printf 'PATH=%s\\n' \"$PATH\""
    )


def _run_probe(sandbox: WorkspaceSandbox, script: str, *, cwd: Path) -> dict[str, str]:
    result = subprocess.run(  # noqa: S603
        sandbox.wrap(["/bin/sh", "-c", script]),
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=dict(sandbox.env),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            fields[name] = value
    return fields


@pytest.mark.parametrize("backend_name", sorted(_BACKEND_BUILDERS))
class TestSandboxConformance:
    """Every backend, probed through the same resource list and assertions."""

    def test_probe_enforces_the_shared_resource_contract(
        self,
        backend_name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        readonly_path = tmp_path / "readonly" / "config.json"
        readonly_path.parent.mkdir()
        readonly_path.write_text('{"k": "v"}\n')
        unlisted_path = tmp_path / "unlisted" / "secret.txt"
        unlisted_path.parent.mkdir()
        unlisted_path.write_text("do not read\n")
        resources = (
            HostResource(readonly_path, HostResourceAccess.READ_ONLY, "conformance fixture"),
        )

        sandbox = _BACKEND_BUILDERS[backend_name](workspace, resources, monkeypatch)
        fields = _run_probe(
            sandbox,
            _probe_script(workspace, readonly_path, unlisted_path),
            cwd=workspace,
        )

        # Write inside the workspace succeeds.
        assert fields["write_ws"] == "0"
        assert (workspace / "written-by-probe.txt").read_text() == "ok"
        # Write to the declared read-only resource fails, and nothing changed.
        assert fields["write_ro"] != "0"
        assert readonly_path.read_text() == '{"k": "v"}\n'
        # The unlisted path is unreadable (or, on bubblewrap, simply absent).
        assert fields["read_unlisted"] != "0"
        # The confined process keeps the caller's uid: no privilege change.
        assert fields["uid"] == str(os.getuid())
        # HOME and PATH inside match what the sandbox promises through env.
        assert fields["HOME"] == sandbox.env["HOME"]
        assert fields["PATH"] == sandbox.env["PATH"]
