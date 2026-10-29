"""Confinement of a host-run job: required, private temp dir, tools hidden."""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

from vs_sandbox.api import (
    HostSandbox,
    LandlockSandbox,
    ProjectPathPolicy,
    SandboxUnavailableError,
    SeatbeltSandbox,
    WorkspaceSandbox,
)
from vs_sandbox.api.slurm import HostJobConfinement, remote_job_prefix

_TMP = "/tmp"  # noqa: S108  # lint-waiver: LW-010108 [S108]; the confined job's private tmpfs path inside its namespace, asserted as data, never opened on the host.


class TestHostJobConfinement:
    def test_a_host_that_cannot_enforce_confinement_refuses_the_job(self, tmp_path: Path) -> None:
        """Enforcement is required: there is no unconfined job."""
        confinement = HostJobConfinement(
            env={"VIBESYS_AGENT_SANDBOX": "0"},
            resources=(),
            project_path_policy=ProjectPathPolicy(),
        )

        with pytest.raises(SandboxUnavailableError) as caught:
            confinement.wrap(tmp_path, ["true"])

        assert str(caught.value) == (
            "[hostsandbox] DISABLED via VIBESYS_AGENT_SANDBOX; agent runs with full "
            "host filesystem access. Sibling runs and host files are reachable."
        )

    def test_the_job_names_bubblewrap_by_program_not_by_the_submit_hosts_path(self) -> None:
        """The job runs on a compute node that need not have the submit host's copy."""
        sandbox = HostSandbox(
            workspace=Path("/shared/candidate"),
            bwrap_path="/submit/operator/bin/bwrap",
        )

        argv = remote_job_prefix(sandbox)

        assert argv[0] == "bwrap"
        assert "/submit/operator/bin" not in " ".join(argv)

    def test_the_job_names_seatbelt_by_program_not_by_the_submit_hosts_path(self) -> None:
        """The inline profile is portable; the submit host's launcher path is not."""
        sandbox = SeatbeltSandbox(
            workspace=Path("/shared/candidate"),
            sandbox_exec_path="/submit/operator/bin/sandbox-exec",
        )

        argv = remote_job_prefix(sandbox)

        assert argv[:2] == ["sandbox-exec", "-p"]
        assert "/submit/operator/bin" not in " ".join(argv)

    @pytest.mark.skipif(sys.platform != "linux", reason="Landlock is a Linux mechanism")
    def test_landlock_is_refused_for_a_job_that_runs_on_another_machine(self) -> None:
        """The submit host's Python path is not a valid compute-node contract."""
        confinement = HostJobConfinement(
            env={
                "VIBESYS_AGENT_SANDBOX": "landlock",
                "PATH": "/usr/bin:/bin",
                "HOME": "/nonexistent/operator-home",
            },
            resources=(),
            project_path_policy=ProjectPathPolicy(),
        )

        with pytest.raises(SandboxUnavailableError) as caught:
            # Root has no granted scratch ancestor, so exact base reaches
            # Landlock's remote-prefix decision instead of failing earlier on
            # the fixture layout. Head refuses before probing the host kernel.
            confinement.wrap(Path("/"), ["true"])

        assert "compute node" in str(caught.value)
        assert "submit host" in str(caught.value)

    def test_a_direct_landlock_backend_is_refused_before_rendering_its_policy(self) -> None:
        """The compatibility boundary also fails closed for a prebuilt backend."""
        sandbox = LandlockSandbox(
            workspace=Path("/shared/candidate"),
            scratch_write_roots=(),
        )

        with pytest.raises(SandboxUnavailableError, match="submit host"):
            remote_job_prefix(sandbox)

    def test_an_unrecognized_backend_is_not_assumed_to_be_remote_compatible(self) -> None:
        """New WorkspaceSandbox implementations must opt into remote execution."""
        sandbox = _UnknownWorkspaceSandbox(workspace=Path("/shared/candidate"))

        with pytest.raises(SandboxUnavailableError, match="no remote-job compatibility"):
            remote_job_prefix(sandbox)

    @pytest.mark.skipif(sys.platform != "linux", reason="bubblewrap is the Linux mechanism")
    def test_the_job_gets_a_private_writable_tmp_and_every_temp_variable_points_at_it(
        self, tmp_path: Path
    ) -> None:
        """Regression: a host TMPDIR absent from the namespace broke ``ncu`` and other tools."""
        tool_dir = tmp_path / "bin"
        tool_dir.mkdir()
        bwrap = tool_dir / "bwrap"
        bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        bwrap.chmod(0o755)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        confinement = HostJobConfinement(
            env={"PATH": str(tool_dir), "HOME": str(tmp_path), "TMPDIR": "/scratch/host-only"},
            resources=(),
            project_path_policy=ProjectPathPolicy(),
        )

        argv = confinement.wrap(workspace, ["true"])

        prefix = argv[: argv.index("--")]
        pairs = list(itertools.pairwise(prefix))
        assert ("--tmpfs", _TMP) in pairs
        setenv = {prefix[i + 1]: prefix[i + 2] for i, arg in enumerate(prefix) if arg == "--setenv"}
        assert setenv == dict.fromkeys(("TMPDIR", "TMP", "TEMP"), _TMP)


class _UnknownWorkspaceSandbox(WorkspaceSandbox):
    """Fake future backend without an explicit remote-job contract."""

    def wrap(self, argv: list[str], cwd: Path | str | None = None) -> list[str]:
        del cwd
        return ["unknown-confinement", *argv]
