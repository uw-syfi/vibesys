"""The startup workspace-root probe is not gated on GPU queue capacity.

Regression for a run whose probe was submitted with the evaluation's 4-GPU,
900 GB request, waited 35 minutes behind other users' GPU jobs, and was
reported as an opaque "infrastructure error".
"""

from __future__ import annotations

import hashlib
import json
import shlex
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    FakeConnector,
    SlurmConfig,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmLocalTransport,
    SlurmWorkspaceRootProbeError,
    minimal_job,
    probe_workspace_root,
)

# test-isolation: the Fake connector's request log is an executable test double outside the API.
from vs_slurm.fake_connector import recorded_commands

if TYPE_CHECKING:
    from collections.abc import Callable
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
# test-isolation: the first attempt's scheduler identity, which the Fake scripts by operation id.
_OPERATION_ID = "workspace-root-probe-" + hashlib.sha256(_RUN_ID.encode()).hexdigest()[:32]
_FIRST_JOB_ID = "5000"  # the Fake's first scheduler identity
_DEFAULT_QUEUE_SECONDS = 1800

# Options a minimal job drops, in every spelling sbatch reads, with any value they take:
# resource requests, start delays, node lists, and abbreviations sbatch would expand.
_DROPPED_SPELLINGS = st.sampled_from(
    [
        ("--gres=gpu:4",),
        ("--gres", "gpu:8"),
        ("--gpus=2",),
        ("-G", "4"),
        ("-G4",),
        ("--gpus-per-node=8",),
        ("--gpus-per-task", "1"),
        ("--tres-per-task=gres/gpu:1",),
        ("-c", "64"),
        ("-c64",),
        ("--cpus-per-task=32",),
        ("--cpus-per-gpu=16",),
        ("--mincpus", "8"),
        ("-B", "2:4:1"),
        ("--sockets-per-node=2",),
        ("--cores-per-socket", "4"),
        ("--threads-per-core=1",),
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
        ("--hold",),
        ("-H",),
        ("--dependency=afterok:7",),
        ("-d", "afterany:8"),
        ("--begin=now+1hour",),
        ("-b", "16:00"),
        ("--array=0-3",),
        ("--signal", "B:USR1@60"),
        ("-w", "node1"),
        ("--nodelist=node[1-2]",),
        ("--part=main",),
        ("--acc", "lab"),
    ]
)
# Placement and identity options the operator chose; a minimal job keeps every one.
_PLACEMENT_SPELLINGS = st.sampled_from(
    [
        ("-p", "priority"),
        ("-ppriority",),
        ("--partition=main",),
        ("--partition", "main"),
        ("-A", "lab"),
        ("-Alab",),
        ("--account=lab",),
        ("-q", "normal"),
        ("--qos=high",),
        ("-C", "b200"),
        ("--constraint", "b200&ib"),
        ("--reservation=campaign",),
        ("-M", "west"),
        ("--clusters=west",),
        ("-x", "node3"),
        ("--exclude", "node[4-5]"),
        ("--wckey=glm",),
        ("--comment", "vibesys probe"),
    ]
)
_MINIMAL_REQUEST = ("--nodes=1", "--ntasks=1", "--cpus-per-task=1", "--mem=1G")


def _config(remote: Path, **settings: object) -> SlurmConfig:
    return SlurmConfig.model_validate(
        {
            "name": "busy",
            "remote_workspace_root": str(remote),
            "transport": SlurmLocalTransport(
                kind="local",
                shell_command=("fake-connector", "shell"),
                rsync_command=("fake-connector", "rsync"),
            ),
            "sbatch_arguments": _EVALUATION_ARGUMENTS,
            "job_timeout_seconds": 2100,
            "poll_interval_seconds": 10.0,
            **settings,
        }
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


def _assert_cleaned(remote: Path) -> None:
    cluster = remote / "busy"
    assert not [path for path in cluster.iterdir() if path.name != ".vibesys-content-cache"]


def test_probe_asks_for_one_cpu_and_no_gpu_in_the_configured_partition(tmp_path: Path) -> None:
    remote, connector = _setup(tmp_path)

    _probe(_config(remote), connector, tmp_path)

    (submitted,) = _submissions(connector)
    assert submitted[:2] == ["-p", "priority"]
    assert not any("gpu" in arg or "900G" in arg or arg == "64" for arg in submitted)
    assert set(_MINIMAL_REQUEST) <= set(submitted)


def test_probe_sbatch_arguments_replace_the_derived_request(tmp_path: Path) -> None:
    remote, connector = _setup(tmp_path)
    override = ("-p", "gpu-only", "--gres=gpu:1", "--time=10")

    _probe(_config(remote, probe_sbatch_arguments=override), connector, tmp_path)

    (submitted,) = _submissions(connector)
    assert tuple(submitted[: len(override)]) == override
    assert not set(_MINIMAL_REQUEST) & set(submitted)


@given(
    dropped=st.lists(_DROPPED_SPELLINGS, max_size=6),
    placement=st.lists(_PLACEMENT_SPELLINGS, max_size=4),
    order=st.randoms(use_true_random=False),
    grace=st.integers(min_value=1, max_value=20),
    exit_wait=st.integers(min_value=1, max_value=1800),
)
@settings(max_examples=examples(pr=50, nightly=500), deadline=None)
def test_a_minimal_job_keeps_exactly_the_placement_options(
    dropped: list[tuple[str, ...]],
    placement: list[tuple[str, ...]],
    order: Random,
    grace: int,
    exit_wait: int,
) -> None:
    groups = [*dropped, *placement]
    order.shuffle(groups)
    config = SlurmConfig(
        name="any",
        remote_workspace_root="/scratch",
        transport=SlurmLocalTransport(kind="local"),
        sbatch_arguments=tuple(token for group in groups for token in group),
        service_stop_grace_seconds=grace,
        process_exit_wait_seconds=exit_wait,
    )

    job = minimal_job(config)

    kept, added = job.arguments[:-5], job.arguments[-5:]
    assert kept == tuple(token for group in groups if group in placement for token in group)
    assert added[:4] == _MINIMAL_REQUEST
    # The minimal job is itself a valid configuration: its time limit leaves the job
    # script's teardown room, plus a minute for the command.
    SlurmConfig.model_validate(
        {**config.model_dump(), "sbatch_arguments": job.arguments}, strict=False
    )
    assert job.time_limit_seconds is not None
    assert job.time_limit_seconds - 60 < grace + exit_wait + 60 <= job.time_limit_seconds
    assert f"{job.time_limit_seconds // 60} min" in job.description


@given(
    queue_timeout=st.none() | st.integers(min_value=1, max_value=1800),
    poll=st.sampled_from([60.0, 300.0]),
    reason=st.sampled_from(["Priority", "Resources", "QOSMaxJobsPerUserLimit", ""]),
)
@settings(max_examples=examples(pr=5, nightly=50), deadline=None)
def test_a_probe_still_queued_at_its_deadline_says_so_and_a_resume_retries_it(
    tmp_path_factory: pytest.TempPathFactory,
    queue_timeout: int | None,
    poll: float,
    reason: str,
) -> None:
    root = tmp_path_factory.mktemp("queued")
    remote, connector = _setup(root)
    connector.script(_OPERATION_ID, states=(SlurmJobStatus.PENDING,), pending_reason=reason)
    config = _config(remote, queue_timeout_seconds=queue_timeout, poll_interval_seconds=poll)
    waited = queue_timeout or _DEFAULT_QUEUE_SECONDS
    why = rf" \(scheduler reason: {reason}\)" if reason else ""
    setting = (
        rf"`queue_timeout_seconds` \(now {queue_timeout} s\)"
        if queue_timeout is not None
        else r"`queue_timeout_seconds` \(unset"
    )

    with pytest.raises(
        SlurmWorkspaceRootProbeError,
        match=(
            rf"probe job {_FIRST_JOB_ID} was never scheduled\. It waited {waited} s in the "
            rf"queue{why} for 1 CPU, 1G of memory and 4 min without GPUs, in the partition "
            rf"`sbatch_arguments` names.*The run retries the probe, and its Slurm work "
            rf"waits for it; raise {setting}"
        ),
    ) as never_scheduled:
        _probe(config, connector, root)
    # It says nothing about the root, so the run may try again (``WorkspaceRootProof``).
    assert never_scheduled.value.retryable
    assert f"scancel {_FIRST_JOB_ID}" in recorded_commands(connector.state)
    _assert_cleaned(remote)

    # Resuming the run retries the probe under a new scheduler identity, which starts.
    _probe(config, connector, root)

    names = [
        next(arg for arg in submitted if arg.startswith("--job-name="))
        for submitted in _submissions(connector)
    ]
    assert len(names) == len(set(names)) == 2
    _assert_cleaned(remote)


def test_a_probe_that_starts_after_its_last_queued_poll_is_not_cancelled(
    tmp_path: Path,
) -> None:
    remote, connector = _setup(tmp_path)
    queue_seconds, poll = 60, 10.0
    polls_in_queue_window = int(queue_seconds / poll) + 1
    connector.script(
        _OPERATION_ID,
        states=(
            *(SlurmJobStatus.PENDING,) * polls_in_queue_window,
            SlurmJobStatus.RUNNING,
            SlurmJobStatus.COMPLETED,
        ),
    )

    _probe(
        _config(remote, queue_timeout_seconds=queue_seconds, poll_interval_seconds=poll),
        connector,
        tmp_path,
    )

    assert f"scancel {_FIRST_JOB_ID}" not in recorded_commands(connector.state)
    assert len(_submissions(connector)) == 1


def _refuse_sbatch(
    request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
) -> dict[str, object]:
    if " sbatch " in f" {request.get('command', '')} ":
        return {
            "version": 1,
            "returncode": 1,
            "stdout": "",
            "stderr": (
                "Welcome to cayenne, private-banner-text\n"
                "sbatch: error: invalid partition specified: nope\n"
            ),
        }
    return answer(request)


def test_an_sbatch_refusal_is_reported_in_sbatchs_own_words_and_a_resume_retries(
    tmp_path: Path,
) -> None:
    remote, connector = _setup(tmp_path)
    connector.intercept(_refuse_sbatch)

    with pytest.raises(
        SlurmWorkspaceRootProbeError,
        match=r"rejected: .*exit code 1 running sbatch: sbatch: error: invalid partition",
    ) as refused:
        _probe(_config(remote), connector, tmp_path)
    assert "private-banner-text" not in str(refused.value)

    # The refusal is a proven rejection, so resuming the run submits the probe again.
    connector.intercept(None)
    _probe(_config(remote), connector, tmp_path)

    assert connector.submitted_job_ids() == (_FIRST_JOB_ID,)
    _assert_cleaned(remote)


_FIELDS_ADDED_SINCE = {
    2: {"generation", "waited_seconds", "pending_reason", "inconclusive_attempts", "job_end"},
    3: {"inconclusive_attempts", "job_end"},
}


@pytest.mark.parametrize("version", sorted(_FIELDS_ADDED_SINCE))
def test_a_record_written_by_an_earlier_release_is_still_read(tmp_path: Path, version: int) -> None:
    remote, connector = _setup(tmp_path)
    config = _config(remote)
    _probe(config, connector, tmp_path)
    # test-isolation: the durable record format is a contract with earlier releases: version 2
    # had no retry generation or queue-wait fields, version 3 no inconclusive attempts.
    record = tmp_path / "probe-state" / "records" / "workspace-root-probe.json"
    current = json.loads(record.read_text(encoding="utf-8"))
    assert current["version"] == 4
    earlier = {
        key: value for key, value in current.items() if key not in _FIELDS_ADDED_SINCE[version]
    }
    record.write_text(json.dumps({**earlier, "version": version}), encoding="utf-8")

    _probe(config, connector, tmp_path)

    assert len(_submissions(connector)) == 1
