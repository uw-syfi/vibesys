"""Every command a job starts after its setup script sees the environment the setup built.

On a real cluster a job inherits the submitting ssh session's environment, and a ``bash -c``
started under it re-reads the operator's login rc files, which export login values (a shared
cache directory) over the setup script's. The Fake executing cluster models both, so a stage
that is a shell would lose the setup's value there exactly as it did on the cluster.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api import ProcessOutcome
from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmFileArtifact,
    SlurmJobRequest,
    SlurmJobRunner,
)

# test-isolation: the Fake connector is an executable test double outside the library API.
from vs_slurm.fake_connector import executing_cluster, handle, login_rc

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_sim.api import OutputSink, ProcessRun, ProcessSpec

_VARIABLE = "SHARED_CACHE"
_PRINT = f'printf %s "${{{_VARIABLE}}}"'
_VALUES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/_.-", min_size=1, max_size=20)


class _InProcessConnector:
    def __init__(self, state: Path) -> None:
        self._state = state

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del timeout, output
        assert spec.input
        answer = handle(self._state, json.loads(spec.input))
        return ProcessOutcome(0, json.dumps(answer).encode(), b"")


def _cluster(root: Path, login_value: str) -> tuple[SlurmJobRunner, Path, Path]:
    state = executing_cluster(root / "cluster")
    login_rc(state, f"export {_VARIABLE}={login_value}\n")
    remote = root / "remote"
    remote.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    config = SlurmConfig(
        name="fake",
        remote_workspace_root=str(remote),
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
    )
    runner = SlurmJobRunner(config, processes=_InProcessConnector(state), clock=ManualClock())
    return runner, workspace, root / "setup.sh"


@given(setup_value=_VALUES, login_value=_VALUES)
@settings(max_examples=examples(pr=3, nightly=30), deadline=None)
def test_each_batch_stage_sees_what_the_setup_script_exported(
    tmp_path_factory: pytest.TempPathFactory, setup_value: str, login_value: str
) -> None:
    root = tmp_path_factory.mktemp("job")
    runner, workspace, setup = _cluster(root, login_value)
    setup.write_text(f"export {_VARIABLE}={setup_value}\n", encoding="utf-8")
    batch = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            setup_script=str(setup),
            stages=(
                SlurmBatchStage(name="shell", command=("bash", "-c", _PRINT)),
                SlurmBatchStage(name="nested", command=("bash", "-c", f"bash -c '{_PRINT}'")),
                SlurmBatchStage(name="program", command=("env",)),
            ),
        )
    )

    result = runner.collect_batch(batch)

    assert [stage.exit_code for stage in result.stages] == [0, 0, 0]
    assert result.stages[0].stdout == setup_value
    assert result.stages[1].stdout == setup_value
    assert f"{_VARIABLE}={setup_value}\n" in result.stages[2].stdout


@given(setup_value=_VALUES, login_value=_VALUES)
@settings(max_examples=examples(pr=3, nightly=30), deadline=None)
def test_a_single_command_sees_what_the_setup_script_exported(
    tmp_path_factory: pytest.TempPathFactory, setup_value: str, login_value: str
) -> None:
    root = tmp_path_factory.mktemp("job")
    runner, workspace, setup = _cluster(root, login_value)
    setup.write_text(f"export {_VARIABLE}={setup_value}\n", encoding="utf-8")
    artifact = root / "collected" / "seen.txt"
    job = runner.submit(
        SlurmJobRequest(
            workspace=workspace,
            setup_script=str(setup),
            command=("bash", "-c", f"{_PRINT} > seen.txt"),
            file_artifacts=(SlurmFileArtifact("seen.txt", artifact),),
        )
    )

    runner.collect(job)

    assert artifact.read_text(encoding="utf-8") == setup_value
