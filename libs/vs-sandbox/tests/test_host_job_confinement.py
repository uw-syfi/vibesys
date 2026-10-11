"""Confinement of a host-run job: required, private temp dir, tools hidden."""

from __future__ import annotations

import itertools
import sys
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api import ProjectPathPolicy, SandboxUnavailableError
from vs_sandbox.api.slurm import HostJobConfinement

if TYPE_CHECKING:
    from pathlib import Path


_TMP = "/tmp"  # noqa: S108  # lint-waiver: LW-010108 [S108]; the confined job's private tmpfs path inside its namespace, asserted as data, never opened on the host.


class TestHostJobConfinement:
    def test_a_host_that_cannot_enforce_confinement_refuses_the_job(self, tmp_path: Path) -> None:
        """Enforcement is required: there is no unconfined job."""
        confinement = HostJobConfinement(
            env={"VIBESYS_AGENT_SANDBOX": "0"},
            resources=(),
            project_path_policy=ProjectPathPolicy(),
        )

        with pytest.raises(SandboxUnavailableError):
            confinement.wrap(tmp_path, ["true"])

    @pytest.mark.skipif(sys.platform != "linux", reason="bubblewrap is the Linux mechanism")
    def test_the_job_names_bubblewrap_by_program_not_by_the_submit_hosts_path(
        self, tmp_path: Path
    ) -> None:
        """The job runs on a compute node that need not have the submit host's copy."""
        tool_dir = tmp_path / "operator-home" / "bin"
        tool_dir.mkdir(parents=True)
        bwrap = tool_dir / "bwrap"
        bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        bwrap.chmod(0o755)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        confinement = HostJobConfinement(
            env={"PATH": str(tool_dir), "HOME": str(tmp_path)},
            resources=(),
            project_path_policy=ProjectPathPolicy(),
        )

        argv = confinement.wrap(workspace, ["true"])

        assert argv[0] == "bwrap"
        assert str(tool_dir) not in " ".join(argv)
        assert argv[-1] == "true"

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
