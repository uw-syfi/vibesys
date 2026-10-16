"""The startup workspace-root probe is not gated on GPU queue capacity.

Regression for a run whose probe was submitted with the evaluation's 4-GPU,
900 GB request, waited 35 minutes behind other users' GPU jobs, and was
reported as an opaque "infrastructure error".
"""

from __future__ import annotations

import hashlib
import shlex
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    FakeConnector,
    SlurmConfig,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmLocalTransport,
    SlurmWorkspaceRootProbeError,
    minimal_sbatch_arguments,
    probe_workspace_root,
)

# test-isolation: the Fake connector's request log is an executable test double outside the API.
from vs_slurm.fake_connector import recorded_commands

if TYPE_CHECKING:
    from pathlib import Path
    from random import Random

_EVALUATION_ARGUMENTS = (
    "-p",
    "priority",
    "--gres=gpu:4",
    "-N",
    "1",
    "-c",
    "64",
    "--mem=900G",
    "-t",
    "00:30:00",
)
_RUN_ID = "run-on-a-busy-cluster"
_OPERATION_ID = "workspace-root-probe-" + hashlib.sha256(_RUN_ID.encode()).hexdigest()[:32]

# Resource requests in every spelling sbatch reads: (tokens, option name).
_RESOURCE_SPELLINGS = st.sampled_from(
    [
        ("--gres=gpu:4",),
        ("--gres", "gpu:8"),
        ("--gpus=2",),
        ("-G", "4"),
        ("-G4",),
        ("--gpus-per-node=8",),
        ("--gpus-per-task", "1"),
        ("-c", "64"),
        ("-c64",),
        ("--cpus-per-task=32",),
        ("--cpus-per-gpu=16",),
        ("--mem=900G",),
        ("--mem", "200G"),
        ("--mem-per-cpu=8G",),
        ("--mem-per-gpu=100G",),
        ("-N", "2"),
        ("-N1",),
        ("--nodes=1",),
        ("-n", "4"),
        ("--ntasks=8",),
        ("--ntasks-per-node=8",),
        ("-t", "00:30:00"),
        ("-t30",),
        ("--time=2:00:00",),
        ("--time", "1-00:00:00"),
        ("--time-min=10",),
        ("--exclusive",),
        ("--exclusive=user",),
    ]
)
# Placement and identity options the operator chose; the probe keeps every one.
_PLACEMENT_SPELLINGS = st.sampled_from(
    [
        ("-p", "priority"),
        ("--partition=main",),
        ("-A", "lab"),
        ("--account=lab",),
        ("-q", "normal"),
        ("--qos=high",),
        ("-C", "b200"),
        ("--reservation=campaign",),
    ]
)
_RESOURCE_NAMES = (
    "--gres",
    "--gpus",
    "--cpus",
    "--mem",
    "--nodes",
    "--ntasks",
    "--time",
    "--exclusive",
)


def _config(
    remote: Path,
    arguments: tuple[str, ...] = _EVALUATION_ARGUMENTS,
    *,
    job_timeout_seconds: int = 2100,
    poll_interval_seconds: float = 10.0,
) -> SlurmConfig:
    return SlurmConfig(
        name="busy",
        remote_workspace_root=str(remote),
        transport=SlurmLocalTransport(
            kind="local",
            shell_command=("fake-connector", "shell"),
            rsync_command=("fake-connector", "rsync"),
        ),
        sbatch_arguments=arguments,
        job_timeout_seconds=job_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def _probe(config: SlurmConfig, connector: FakeConnector, root: Path) -> None:
    clock = ManualClock()
    runner = SlurmJobRunner(
        config, processes=connector, scratch_root=root / "probe-state", clock=clock
    )
    probe_workspace_root(
        runner, state_root=root / "probe-state", run_id=_RUN_ID, pause=clock.advance
    )


def _submissions(connector: FakeConnector) -> list[list[str]]:
    submitted = []
    for command in recorded_commands(connector.state):
        tokens = shlex.split(command)
        if "sbatch" in tokens:
            submitted.append(tokens[tokens.index("sbatch") + 1 :])
    return submitted


def _setup(root: Path) -> tuple[Path, FakeConnector]:
    remote = root / "remote"
    remote.mkdir()
    return remote, FakeConnector(root / "cluster")


def test_probe_asks_for_one_cpu_and_no_gpu_in_the_configured_partition(tmp_path: Path) -> None:
    remote, connector = _setup(tmp_path)

    _probe(_config(remote), connector, tmp_path)

    (submitted,) = _submissions(connector)
    assert submitted[:2] == ["-p", "priority"]
    assert not any("gpu" in arg or "900G" in arg or arg == "64" for arg in submitted)
    assert {"--nodes=1", "--ntasks=1", "--cpus-per-task=1", "--mem=1G"} <= set(submitted)


@given(
    resources=st.lists(_RESOURCE_SPELLINGS, max_size=6),
    placement=st.lists(_PLACEMENT_SPELLINGS, max_size=4),
    order=st.randoms(use_true_random=False),
    grace=st.integers(min_value=1, max_value=20),
    exit_wait=st.integers(min_value=1, max_value=1800),
)
@settings(max_examples=examples(pr=50, nightly=500), deadline=None)
def test_minimal_arguments_drop_every_resource_request_and_keep_placement(
    resources: list[tuple[str, ...]],
    placement: list[tuple[str, ...]],
    order: Random,
    grace: int,
    exit_wait: int,
) -> None:
    groups = [*resources, *placement]
    order.shuffle(groups)
    arguments = tuple(token for group in groups for token in group)
    config = SlurmConfig(
        name="any",
        remote_workspace_root="/scratch",
        transport=SlurmLocalTransport(kind="local"),
        sbatch_arguments=arguments,
        service_stop_grace_seconds=grace,
        process_exit_wait_seconds=exit_wait,
    )

    minimal = minimal_sbatch_arguments(config)

    kept, added = minimal[:-5], minimal[-5:]
    placed = tuple(token for group in groups if group in placement for token in group)
    assert kept == placed
    assert not any(token.startswith(_RESOURCE_NAMES) for token in kept)
    assert added[:4] == ("--nodes=1", "--ntasks=1", "--cpus-per-task=1", "--mem=1G")
    # The minimal job is itself a valid configuration: its time limit leaves the job
    # script's teardown room, plus a minute for the command.
    reshaped = config.model_copy(update={"sbatch_arguments": minimal})
    SlurmConfig.model_validate(reshaped.model_dump(), strict=False)
    minutes = int(added[4].removeprefix("--time="))
    assert (minutes - 1) * 60 < grace + exit_wait + 60 <= minutes * 60


@given(
    timeout=st.integers(min_value=1, max_value=1800),
    poll=st.sampled_from([60.0, 300.0]),
    reason=st.sampled_from(["Priority", "Resources", "QOSMaxJobsPerUserLimit"]),
)
@settings(
    max_examples=examples(pr=5, nightly=50),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_a_probe_still_queued_at_its_deadline_says_it_was_never_scheduled(
    tmp_path_factory: pytest.TempPathFactory, timeout: int, poll: float, reason: str
) -> None:
    root = tmp_path_factory.mktemp("queued")
    remote, connector = _setup(root)
    connector.script(_OPERATION_ID, states=(SlurmJobStatus.PENDING,), pending_reason=reason)
    config = _config(remote, job_timeout_seconds=timeout, poll_interval_seconds=poll)
    job_id = "5000"  # the Fake's first scheduler identity

    expected = (
        rf"probe job {job_id} was never scheduled\. It waited {timeout} s in the queue "
        rf"\(scheduler reason: {reason}\).*Relaunch when that partition has a free CPU, "
        r"or raise `job_timeout_seconds`"
    )
    with pytest.raises(SlurmWorkspaceRootProbeError, match=expected):
        _probe(config, connector, root)
    assert f"scancel {job_id}" in recorded_commands(connector.state)
    cluster = remote / "busy"
    assert not [path for path in cluster.iterdir() if path.name != ".vibesys-content-cache"]

    # A restart of the same run reports the same recorded outcome without resubmitting.
    with pytest.raises(SlurmWorkspaceRootProbeError, match=expected):
        _probe(config, connector, root)
    assert len(_submissions(connector)) == 1
