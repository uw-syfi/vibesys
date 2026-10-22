"""Opt-in real Docker proof, through the environment owner on every agent image base."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest

from vs_project.api import Project
from vs_proto.api import replay
from vs_runtime.api.infrastructure import DockerEnvironment, DockerEnvironmentConfig
from vs_runtime.api.replay import WriterRegistry
from vs_sandbox.api import RUN_ID_LABEL, SubprocessDockerCli
from vs_sim.api import DISCARD, OsThreads

if TYPE_CHECKING:
    from pathlib import Path

    from tests.minimal_container.editor import Editor

pytestmark = [pytest.mark.minimal_container, pytest.mark.real_contract]


def test_environment_proof_removes_writable_domain_and_its_descendants(
    editor: Editor, tmp_path: Path
) -> None:
    system = editor.request.system
    docker = SubprocessDockerCli(processes=system.processes, launcher=system.launcher)
    run = partial(docker.run, timeout_seconds=30, output=DISCARD)
    listed = run(
        ["docker", "ps", "-aq", "--filter", f"label={RUN_ID_LABEL}={editor.request.run_id}"]
    )
    assert listed.returncode == 0, listed.stderr
    (editor_id,) = listed.stdout.splitlines()
    image = run(["docker", "inspect", "--format", "{{.Config.Image}}", editor_id])
    assert image.returncode == 0, image.stderr
    daemon = run(["docker", "info", "--format", "{{.ID}}"])
    assert daemon.returncode == 0, daemon.stderr
    backing = replay.WriterBacking(
        environment="docker", identity="contract-root", authority=daemon.stdout.strip()
    )
    ledger = WriterRegistry(
        Project.open(tmp_path).state.state_store_namespace("writers"), backing, OsThreads()
    )
    ledger.create()
    started = run(
        [
            "docker",
            "run",
            "-d",
            "--user",
            "0",
            "-v",
            f"{tmp_path}:/workspace",
            image.stdout.strip(),
            "bash",
            "-c",
            "sleep 600",
        ]
    )
    assert started.returncode == 0, started.stderr
    domain = started.stdout.strip()
    try:
        ledger.grant(
            replay.WriterIdentity(
                run_id="writer-run",
                host_incarnation="host-a/1",
                physical_id="physical-child",
                domain_id=domain,
            )
        )
        # The exec reply is the readiness handshake: no timing verdict or polling.
        child = run(
            [
                "docker",
                "exec",
                domain,
                "bash",
                "-c",
                "printf 'before proof' > /workspace/written; sleep 600 >/dev/null 2>&1 & echo $! > /tmp/writer.pid",
            ]
        )
        assert child.returncode == 0, child.stderr
        assert (tmp_path / "written").read_text() == "before proof"
        environment = DockerEnvironment(DockerEnvironmentConfig(docker=docker))
        result = environment.reconcile_writers(ledger.close(), system=system, log=lambda _: None)
        assert result.status is replay.WriterReconciliationStatus.PROVEN_STOPPED, result.detail
        ledger.record(result)
        assert ledger.require_proof().absent_domains == [domain]
        absent = run(["docker", "inspect", domain])
        assert absent.returncode != 0
        assert (
            environment.reconcile_writers(ledger.close(), system=system, log=lambda _: None)
            == result
        )
    finally:
        run(["docker", "rm", "-f", domain])
