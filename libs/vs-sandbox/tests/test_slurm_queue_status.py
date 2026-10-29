"""The Slurm queue status reader shows the run its own jobs and only counts everyone else's.

The mapping from one scheduler reading to the run's view is a pure function, so its
guarantees are checked as properties over generated readings. The reader passes the
shared ``QueueStatusReader`` contract, and is driven once end to end through the
production ``SlurmJobRunner`` over the executable connector Fake.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_evaluation.api.queue_status import QueueItemKind, QueueItemState, QueueStatus
from vs_evaluation.api.testing import QueueStatusReaderContract, QueueStatusReaderUnderTest
from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    SlurmQueueStatusReader,
    queue_status_from_snapshot,
)
from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    NodeGpus,
    OwnJob,
    QueuedJob,
    QueueSnapshot,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmError,
    SlurmJobRunner,
    parse_queue_snapshot,
    split_framed_scheduler_query,
)

# test-isolation: public executable transport Fake answers as the scheduler does.
from vs_slurm.fake_connector import FakeConnector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PREFIX = "vibesys-gpu-3f2a9c1d"
RECORDED = """\
2026-10-15T09:30:00
__VS_QUEUE_MINE__
7001|PENDING|Priority|2026-10-15T09:20:00|2026-10-15T10:05:00|gpu|1200|vs-eval-a1b2
7002|RUNNING|None|2026-10-15T09:00:00|2026-10-15T09:10:00|gpu|1500|vibesys-gpu-3f2a9c1d-capture-0a1b2c3d
7003|PENDING|Resources|2026-10-15T09:29:00|N/A|gpu|900|another-run-of-mine
__VS_QUEUE_EVERYONE__
PENDING|gpu|1200
RUNNING|gpu|1500
PENDING|gpu|900
PENDING|gpu|5000
RUNNING|cpu|10
__VS_QUEUE_NODES__
gpu-node-01|gpu*|mix|gpu:h100:8(S:0-1)|gpu:h100:6(IDX:0-5)|
gpu-node-02|gpu*|drain*|gpu:h100:8(S:0-1)|gpu:h100:0(IDX:N/A)|
cpu-node-01|cpu|idle|(null)|gpu:0|
"""


class _RecordedQueue:
    """A scheduler whose every reading is one recorded answer, or one failure."""

    def __init__(self, text: str | None) -> None:
        self._text = text

    def read_queue(self) -> QueueSnapshot:
        if self._text is None:
            raise SlurmError.truncated_output()
        return parse_queue_snapshot(self._text)


def _reader(queue: _RecordedQueue, ids: frozenset[str]) -> SlurmQueueStatusReader:
    return SlurmQueueStatusReader(
        queue, evaluation_job_ids=lambda: ids, agent_job_prefix=PREFIX, partitions=frozenset()
    )


class TestSlurmReader(QueueStatusReaderContract):
    """Each seed records a different set of evaluation jobs, or a scheduler that fails."""

    def reader_under_test(self, seed: int) -> QueueStatusReaderUnderTest:
        ids = frozenset(job for bit, job in enumerate(("7001", "7003", "9999")) if seed >> bit & 1)
        queue = _RecordedQueue(None if seed == 0 else RECORDED)
        expected = (
            None
            if seed == 0
            else queue_status_from_snapshot(
                parse_queue_snapshot(RECORDED),
                evaluation_job_ids=ids,
                agent_job_prefix=PREFIX,
                partitions=frozenset(),
            )
        )
        return QueueStatusReaderUnderTest(_reader(queue, ids), expected=expected)


def test_a_recorded_reading_lists_the_runs_own_jobs_and_counts_the_partition() -> None:
    reading = _reader(_RecordedQueue(RECORDED), frozenset({"7001"})).read()

    assert isinstance(reading, QueueStatus)
    assert reading.items is not None
    evaluation, agent = reading.items
    assert (evaluation.id, evaluation.kind, evaluation.state) == (
        "7001",
        QueueItemKind.EVALUATION,
        QueueItemState.WAITING,
    )
    assert evaluation.waited_seconds == 600
    assert evaluation.estimated_start_in_seconds == 2100
    assert evaluation.jobs_ahead == 1  # only the waiting job at priority 5000 is ahead
    assert evaluation.wait_reason == "Priority"
    assert (agent.id, agent.kind, agent.state) == ("7002", QueueItemKind.AGENT_GPU, "running")
    assert (agent.waited_seconds, agent.running_seconds) == (600, 1200)
    # Only the gpu partition the run's jobs use is counted; the drained node is not.
    contention = reading.contention
    assert (contention.jobs_waiting, contention.jobs_running) == (3, 1)
    assert (contention.gpus_total, contention.gpus_busy, contention.gpus_free) == (8, 6, 2)


def test_a_failed_scheduler_read_is_unavailable_with_its_reason() -> None:
    reading = _reader(_RecordedQueue(None), frozenset()).read()

    assert reading.status == "unavailable"
    assert "SlurmError" in reading.reason


def test_the_reader_reads_through_the_production_runner(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "connector")

    def answer(
        request: dict[str, object], forward: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        query, end = split_framed_scheduler_query(str(request.get("command", "")))
        if "__VS_QUEUE_MINE__" not in query:
            return forward(request)
        return {"version": 1, "returncode": 0, "stdout": RECORDED + end, "stderr": ""}

    connector.intercept(answer)
    remote = tmp_path / "remote"
    remote.mkdir()
    runner = SlurmJobRunner(
        SlurmConfig(
            name="queue",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        ),
        processes=connector,
        clock=ManualClock(),
    )

    reading = SlurmQueueStatusReader(
        runner,
        evaluation_job_ids=lambda: frozenset({"7001"}),
        agent_job_prefix=None,
        partitions=frozenset({"gpu"}),
    ).read()

    assert isinstance(reading, QueueStatus)
    assert [item.id for item in (reading.items or ())] == ["7001"]


_NOW = datetime.fromisoformat("2026-10-15T12:00:00")
_TIME = st.none() | st.integers(-86_400, 86_400).map(lambda s: _NOW + timedelta(seconds=s))
_PARTS = st.frozensets(st.sampled_from(["gpu", "debug", "cpu"]), min_size=1, max_size=2)
_STATE = st.sampled_from(["PENDING", "RUNNING", "CONFIGURING"])
_PRIORITY = st.none() | st.integers(0, 10_000)
_NAME = st.sampled_from([f"{PREFIX}-0a1b2c3d4e5f", f"{PREFIX}x-1", "vibesys-gpu-other-1", "eval"])


@st.composite
def _snapshots(draw: st.DrawFn) -> QueueSnapshot:
    mine = draw(
        st.lists(
            st.builds(
                OwnJob,
                job_id=st.integers(1, 40).map(str),
                name=_NAME,
                state=_STATE,
                reason=st.none() | st.text(min_size=1, max_size=100),
                submitted=_TIME,
                start=_TIME,
                partitions=_PARTS,
                priority=_PRIORITY,
            ),
            max_size=6,
        )
    )
    everyone = draw(
        st.lists(
            st.builds(QueuedJob, state=_STATE, partitions=_PARTS, priority=_PRIORITY), max_size=12
        )
    )
    nodes = draw(
        st.lists(
            st.builds(
                NodeGpus,
                node=st.sampled_from(["n1", "n2", "n3"]),
                partition=st.sampled_from(["gpu", "debug", "cpu"]),
                state=st.sampled_from(["idle", "mix", "alloc", "drain", "down"]),
                gpus=st.integers(0, 8),
                gpus_used=st.integers(0, 10),
            ),
            max_size=6,
        )
    )
    return QueueSnapshot(now=_NOW, mine=tuple(mine), everyone=tuple(everyone), nodes=tuple(nodes))


@settings(max_examples=examples(pr=50, nightly=1000))
@given(
    snapshot=_snapshots(),
    ids=st.frozensets(st.integers(1, 40).map(str), max_size=10),
    with_prefix=st.booleans(),
    partitions=st.frozensets(st.sampled_from(["gpu", "debug", "cpu"]), max_size=2),
)
def test_items_are_only_the_runs_own_jobs_and_counts_are_consistent(
    snapshot: QueueSnapshot,
    ids: frozenset[str],
    *,
    with_prefix: bool,
    partitions: frozenset[str],
) -> None:
    prefix = PREFIX if with_prefix else None

    status = queue_status_from_snapshot(
        snapshot, evaluation_job_ids=ids, agent_job_prefix=prefix, partitions=partitions
    )

    own = [
        job
        for job in snapshot.mine
        if job.job_id in ids or (prefix is not None and job.name.startswith(f"{prefix}-"))
    ]
    assert status.items is not None
    assert [item.id for item in status.items] == [job.job_id for job in own]
    for item, job in zip(status.items, own, strict=True):
        expected_kind = QueueItemKind.EVALUATION if job.job_id in ids else QueueItemKind.AGENT_GPU
        assert item.kind is expected_kind
        assert (item.state is QueueItemState.WAITING) == (job.state == "PENDING")
        if item.jobs_ahead is not None:
            assert item.jobs_ahead <= len(snapshot.everyone)
    contention = status.contention
    assert contention.jobs_waiting is not None
    assert contention.jobs_running is not None
    assert contention.jobs_waiting + contention.jobs_running <= len(snapshot.everyone)
    assert contention.gpus_total == (contention.gpus_busy or 0) + (contention.gpus_free or 0)


@settings(max_examples=examples(pr=50, nightly=1000))
@given(
    prefix=st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", fullmatch=True),
    run_ids=st.lists(st.text(min_size=1, max_size=40), min_size=2, max_size=2, unique=True),
)
def test_each_run_gets_its_own_valid_agent_gpu_job_prefix(prefix: str, run_ids: list[str]) -> None:
    config = AgentGpuConfig(
        partitions=("gpu",), max_gpus=1, max_time_minutes=60, job_name_prefix=prefix
    )

    first, second = (config.for_run(run_id).job_name_prefix for run_id in run_ids)

    assert first == config.for_run(run_ids[0]).job_name_prefix
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", first)
    assert first.startswith(prefix[:23])
    # Two runs differ unless their 32-bit digests collide, which these few never do.
    assert first != second
