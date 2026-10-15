"""The read-only queue query: what it runs, how its answer parses, and how it fails.

The parser is checked on recorded ``squeue``/``sinfo`` text and on generated rows; the
runner's ``read_queue`` is driven through the executable connector Fake answering the
one framed query, so the same transport and framing as production are exercised.
"""

from __future__ import annotations

import shlex
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    QueueViewError,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmError,
    SlurmJobRequest,
    SlurmJobRunner,
    gpu_count,
    parse_queue_snapshot,
    queue_snapshot_command,
    recorded_job_ids,
    split_framed_scheduler_query,
)

# test-isolation: public executable transport Fake answers as the scheduler does.
from vs_slurm.fake_connector import FakeConnector

# test-isolation: public wiring constructs the production cluster.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

type Request = dict[str, object]
type Intercept = Callable[[Request, Callable[[Request], Request]], Request]

# Recorded from a Slurm 23.11 cluster (ids, names and nodes changed), in the query's format.
RECORDED = """\
2026-10-15T09:30:00
__VS_QUEUE_MINE__
7001|PENDING|Priority|2026-10-15T09:20:00|2026-10-15T10:05:00|gpu|1200|vs-eval-a1b2
7002|RUNNING|None|2026-10-15T09:00:00|2026-10-15T09:10:00|gpu,debug|1500|vibesys-gpu-3f2a9c1d-0a1b2c3d4e5f
7003|PENDING|Resources|2026-10-15T09:29:00|N/A|gpu|900|name|with|bars
__VS_QUEUE_EVERYONE__
PENDING|gpu|1200
RUNNING|gpu,debug|1500
PENDING|gpu|900
PENDING|gpu|5000
RUNNING|cpu|10
__VS_QUEUE_NODES__
gpu-node-01|gpu*|mix|gpu:h100:8(S:0-1)|gpu:h100:6(IDX:0-5)|
gpu-node-02|gpu*|drain*|gpu:h100:8(S:0-1)|gpu:h100:0(IDX:N/A)|
gpu-node-02|debug|drain*|gpu:h100:8(S:0-1)|gpu:h100:0(IDX:N/A)|
cpu-node-01|cpu|idle|(null)|gpu:0|
"""


def test_a_recorded_reading_parses_into_own_jobs_anonymous_jobs_and_nodes() -> None:
    snapshot = parse_queue_snapshot(RECORDED)

    assert snapshot.now == datetime.fromisoformat("2026-10-15T09:30:00")
    first, second, third = snapshot.mine
    assert (first.job_id, first.state, first.reason, first.priority) == (
        "7001",
        "PENDING",
        "Priority",
        1200,
    )
    assert first.start == datetime.fromisoformat("2026-10-15T10:05:00")
    assert second.partitions == frozenset({"gpu", "debug"})
    assert second.reason is None
    assert third.start is None
    assert third.name == "name|with|bars"
    assert [job.state for job in snapshot.everyone].count("PENDING") == 3
    assert {(node.node, node.partition, node.gpus, node.gpus_used) for node in snapshot.nodes} == {
        ("gpu-node-01", "gpu", 8, 6),
        ("gpu-node-02", "gpu", 8, 0),
        ("gpu-node-02", "debug", 8, 0),
        ("cpu-node-01", "cpu", 0, 0),
    }
    assert [node.schedulable for node in snapshot.nodes] == [True, False, False, True]


def test_the_query_runs_only_programs_that_read() -> None:
    programs = {
        next(token for token in shlex.split(part) if "=" not in token)
        for part in queue_snapshot_command().split("&&")
    }

    assert programs == {"date", "echo", "squeue", "sinfo"}


_TIMES = st.datetimes(
    min_value=datetime.fromisoformat("2020-01-01T00:00:00"),
    max_value=datetime.fromisoformat("2040-01-01T00:00:00"),
).map(lambda moment: moment.replace(microsecond=0))
_WORD = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=12)
_PARTITIONS = st.frozensets(_WORD, min_size=1, max_size=3)


def _stamp(moment: datetime | None) -> str:
    return "N/A" if moment is None else moment.strftime("%Y-%m-%dT%H:%M:%S")


@settings(max_examples=examples(pr=30, nightly=500))
@given(
    now=_TIMES,
    mine=st.lists(
        st.tuples(
            st.integers(1, 10**9),
            st.sampled_from(["PENDING", "RUNNING", "CONFIGURING"]),
            st.none() | _WORD,
            st.none() | _TIMES,
            st.none() | _TIMES,
            _PARTITIONS,
            st.none() | st.integers(0, 10**6),
            st.text(alphabet="ab|-_ ", max_size=20),
        ),
        max_size=5,
    ),
    everyone=st.lists(
        st.tuples(st.sampled_from(["PENDING", "RUNNING"]), _PARTITIONS, st.integers(0, 10**6)),
        max_size=8,
    ),
    nodes=st.lists(st.tuples(_WORD, _WORD, st.integers(0, 16), st.integers(0, 16)), max_size=6),
)
def test_generated_rows_read_back_as_written(
    now: datetime,
    mine: list[
        tuple[
            int, str, str | None, datetime | None, datetime | None, frozenset[str], int | None, str
        ]
    ],
    everyone: list[tuple[str, frozenset[str], int]],
    nodes: list[tuple[str, str, int, int]],
) -> None:
    text = "\n".join(
        [
            _stamp(now),
            "__VS_QUEUE_MINE__",
            *(
                f"{job}|{state}|{reason or 'None'}|{_stamp(submitted)}|{_stamp(start)}|"
                f"{','.join(sorted(parts))}|{'' if priority is None else priority}|{name}"
                for job, state, reason, submitted, start, parts, priority, name in mine
            ),
            "__VS_QUEUE_EVERYONE__",
            *(
                f"{state}|{','.join(sorted(parts))}|{priority}"
                for state, parts, priority in everyone
            ),
            "__VS_QUEUE_NODES__",
            *(
                f"{node}|{partition}|mix|gpu:{gpus}|gpu:x:{used}(IDX:0)|"
                for node, partition, gpus, used in nodes
            ),
        ]
    )

    snapshot = parse_queue_snapshot(text)

    assert snapshot.now == now
    assert [
        (job.job_id, job.state, job.submitted, job.start, job.partitions, job.priority)
        for job in snapshot.mine
    ] == [
        (str(job), state, sub, start, parts, prio)
        for job, state, _, sub, start, parts, prio, _ in mine
    ]
    assert [job.name for job in snapshot.mine] == [row[-1].strip() for row in mine]
    assert [(job.state, job.partitions, job.priority) for job in snapshot.everyone] == everyone
    assert [(node.node, node.partition, node.gpus, node.gpus_used) for node in snapshot.nodes] == [
        (node, partition, gpus, used) for node, partition, gpus, used in nodes
    ]


@settings(max_examples=examples(pr=30, nightly=500))
@given(cut=st.integers(0, len(RECORDED)))
def test_an_answer_cut_before_its_last_section_is_refused(cut: int) -> None:
    text = RECORDED[:cut]
    try:
        snapshot = parse_queue_snapshot(text)
    except QueueViewError:
        return
    # Only a cut that keeps every section marker may parse; the transport's end line,
    # not the parser, refuses a cut inside the last section.
    assert "__VS_QUEUE_NODES__" in text
    assert len(snapshot.mine) == 3


@pytest.mark.parametrize(
    ("gres", "count"),
    [
        ("(null)", 0),
        ("gpu:4", 4),
        ("gpu:a100:4(S:0-1)", 4),
        ("gpu:a100:4(S:0-1),gpu:2", 6),
        ("gpu:h100:8(IDX:0-7),shard:16", 8),
        ("license:2", 0),
        ("gpu", 0),
    ],
)
def test_gpu_count_reads_every_gres_spelling(gres: str, count: int) -> None:
    assert gpu_count(gres) == count


def _runner(base: Path, connector: FakeConnector) -> SlurmJobRunner:
    remote = base / "remote"
    remote.mkdir(exist_ok=True)
    return SlurmJobRunner(
        SlurmConfig(
            name="queue",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        ),
        processes=connector,
        clock=ManualClock(),
    )


def _answering(stdout: str, *, whole: bool = True) -> Intercept:
    """Answer the queue query with ``stdout``; forward every other request."""

    def answer(request: Request, forward: Callable[[Request], Request]) -> Request:
        query, end = split_framed_scheduler_query(str(request.get("command", "")))
        if "__VS_QUEUE_MINE__" not in query:
            return forward(request)
        return {
            "version": 1,
            "returncode": 0,
            "stdout": stdout + (end if whole else ""),
            "stderr": "",
        }

    return answer


def test_read_queue_sends_the_one_query_and_parses_its_answer(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "connector")
    connector.intercept(_answering(RECORDED))

    snapshot = _runner(tmp_path, connector).read_queue()

    assert [job.job_id for job in snapshot.mine] == ["7001", "7002", "7003"]
    assert len(snapshot.everyone) == 5


def test_read_queue_refuses_an_answer_that_did_not_arrive_whole(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "connector")
    connector.intercept(_answering(RECORDED, whole=False))

    with pytest.raises(SlurmError):
        _runner(tmp_path, connector).read_queue()


def test_read_queue_refuses_an_answer_of_another_shape(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "connector")
    connector.intercept(_answering("squeue: error: invalid option --me\n"))

    with pytest.raises(QueueViewError):
        _runner(tmp_path, connector).read_queue()


def test_recorded_job_ids_names_the_jobs_this_cluster_submitted(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "connector")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cluster = SlurmCluster(_runner(tmp_path, connector), state_root=tmp_path / "identity")

    before = recorded_job_ids(tmp_path / "identity")
    cluster.submit(SlurmJobRequest(workspace=workspace, command=("true",)), operation_id="op")

    assert before == frozenset()
    # The executing Fake numbers its first job 5000.
    assert recorded_job_ids(tmp_path / "identity") == frozenset({"5000"})
    assert recorded_job_ids(tmp_path / "missing") == frozenset()


def test_a_reading_is_relative_to_the_scheduler_clock() -> None:
    snapshot = parse_queue_snapshot(RECORDED)
    pending = snapshot.mine[0]

    assert pending.submitted is not None
    assert pending.start is not None
    assert snapshot.now - pending.submitted == timedelta(minutes=10)
    assert pending.start - snapshot.now == timedelta(minutes=35)
