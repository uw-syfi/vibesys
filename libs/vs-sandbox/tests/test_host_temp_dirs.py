"""A host scratch directory named by TMPDIR must be writable inside a confined job.

Regression: a login shell in the job re-ran ``/etc/profile.d`` and reset ``TMPDIR`` to a
host path (``/raid/tmp``) that the namespace lacked, so ``ncu`` could not open it.
"""

from __future__ import annotations

import sys
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

import vs_sandbox.api as sandbox_api
from vs_sandbox.api import HostSandbox
from vs_sim.api import ProbeResult
from vs_sim.api.testing import ScriptedProbe

if TYPE_CHECKING:
    from pathlib import Path

_PRIVATE_TMP = "/tmp"  # noqa: S108  # lint-waiver: LW-010987 [S108]; the job's private tmpfs path inside its namespace, asserted as data, never opened on the host.


def _tmpfs_mounts(sandbox: HostSandbox) -> set[str]:
    return {b for a, b in pairwise(sandbox.wrap(["true"])) if a == "--tmpfs"}


def _build(
    workspace: Path,
    *,
    env: dict[str, str],
    login_shell_output: str | None,
) -> HostSandbox:
    """Build a host sandbox whose login shell prints *login_shell_output* (``None``: cannot run)."""

    def script(argv: tuple[str, ...]) -> ProbeResult | None:
        if argv[:2] == ("bash", "-lc"):
            return None if login_shell_output is None else ProbeResult(0, login_shell_output)
        return ProbeResult(0, "")  # the bwrap dry run

    # The probe answers the dry run, so only the program's presence on PATH matters.
    bin_dir = workspace.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    bwrap = bin_dir / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bwrap.chmod(0o755)
    sandbox = sandbox_api.build_host_sandbox(
        workspace, env={**env, "PATH": str(bin_dir)}, probe=ScriptedProbe(script)
    )
    assert isinstance(sandbox, HostSandbox)
    return sandbox


@pytest.mark.skipif(sys.platform == "darwin", reason="bubblewrap is the Linux mechanism")
def test_a_tmpdir_set_only_by_the_login_shell_is_a_private_tmpfs_in_the_job(tmp_path: Path) -> None:
    sandbox = _build(tmp_path, env={}, login_shell_output="/raid/tmp\n/raid/tmp\n/raid/tmp\n")

    assert _tmpfs_mounts(sandbox) >= {_PRIVATE_TMP, "/raid/tmp"}


@pytest.mark.skipif(sys.platform == "darwin", reason="bubblewrap is the Linux mechanism")
def test_a_tmpdir_in_the_brokers_environment_is_a_private_tmpfs_even_without_a_login_shell(
    tmp_path: Path,
) -> None:
    sandbox = _build(tmp_path, env={"TMPDIR": "/scratch/host"}, login_shell_output=None)

    assert _tmpfs_mounts(sandbox) >= {_PRIVATE_TMP, "/scratch/host"}


@pytest.mark.parametrize("junk", ["", "/", "relative/tmp", "   ", "/raid/../"])
@pytest.mark.skipif(sys.platform == "darwin", reason="bubblewrap is the Linux mechanism")
def test_only_absolute_non_root_directories_are_mounted(junk: str, tmp_path: Path) -> None:
    sandbox = _build(
        tmp_path,
        env={"TMPDIR": junk},
        login_shell_output=f"{junk}\n/raid//tmp/\n{junk}\n",
    )

    assert _tmpfs_mounts(sandbox) == {_PRIVATE_TMP, "/raid/tmp"}
