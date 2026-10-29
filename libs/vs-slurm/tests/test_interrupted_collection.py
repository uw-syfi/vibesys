"""A download cut short while a finished job is collected settles nothing and scores nothing.

A batch that took tens of minutes has finished; its results are whole on the
cluster. Collecting them downloads the job's status file, its log, the stage
result tree and each passed stage's artifacts. When the connection drops during
any of those downloads (before the first file, mid-tree, or after the last file
but before the transfer reports success), the local copy proves nothing about
the remote files. So the collection reads no result (``ClusterUnknown`` without
one), its ledger attempt cannot settle and stays collectable, and a later
collection reads the whole results and settles the attempt completed.

A result path the cluster answers is absent is a different thing: a missing
result, which settles the attempt failed (the cluster contract suite covers it).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vs_sim.api.testing import ManualClock
from vs_slurm.api import (
    ClusterCollected,
    ClusterSubmitted,
    ClusterUnknown,
    FakeEvaluationLedger,
    LedgerLease,
    LedgerRecord,
    LedgerState,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmFileArtifact,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmTreeArtifact,
    job_of_handle,
    ledger_operation_id,
    settle,
    settlement,
)

# test-isolation: public executable transport Fake runs the job and answers each download.
from vs_slurm.fake_connector import FakeConnector

# test-isolation: public wiring constructs the production cluster.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

type Request = dict[str, object]
type Answer = Callable[[Request], dict[str, object]]
# How much of one download lands before the connection drops: that many of its
# files (in path order), half of its one file's bytes, or all of it.
type Landed = int | Literal["half_file", "all"]

PROJECT = "demo-0123456789ab"
_KEY = "c" * 64
_OPERATION = ledger_operation_id(_KEY, 0)
_DROPPED = {"version": 1, "returncode": 12, "stdout": "", "stderr": "connection reset by peer"}
_BENCHMARK = (
    "mkdir -p profile && printf a > profile/a.txt && printf b > profile/b.txt"
    " && printf 'score 42' | tee score.txt"
)


@dataclass
class _Drop:
    """Answers every request, except that download number ``which`` drops after ``landed``."""

    which: int | None = None
    landed: Landed = 0
    gets: list[Request] = field(default_factory=list)

    def __call__(self, request: Request, answer: Answer) -> dict[str, object]:
        if request["operation"] != "get":
            return answer(request)
        self.gets.append(request)
        if len(self.gets) - 1 != self.which:
            return answer(request)
        _land(Path(str(request["remote_path"])), Path(str(request["local_path"])), self.landed)
        return dict(_DROPPED)


def _files(remote: Path) -> list[Path]:
    return sorted(path for path in remote.rglob("*") if path.is_file()) if remote.is_dir() else []


def _land(remote: Path, local: Path, landed: Landed) -> None:
    """Copy what a transfer cut at ``landed`` left behind, as rsync's temp-and-rename would."""
    if remote.is_file():
        if landed == "all":
            shutil.copyfile(remote, local)
        elif landed == "half_file":
            data = remote.read_bytes()
            local.write_bytes(data[: len(data) // 2])
        return
    files = _files(remote)
    count = len(files) if landed == "all" else landed if isinstance(landed, int) else 0
    for path in files[:count]:
        target = local / path.relative_to(remote)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def _cut_points(remote: Path) -> Iterator[Landed]:
    """Every place one download can drop: before any file, after each file, after all."""
    if remote.is_file():
        yield from (0, "half_file", "all")
        return
    yield from range(len(_files(remote)))
    yield "all"


@dataclass
class _World:
    cluster: SlurmCluster
    drop: _Drop
    handle: SlurmBatchHandle
    collected_root: Path
    ledger: FakeEvaluationLedger

    def collect(self) -> ClusterCollected | ClusterUnknown:
        # A retry collects over whatever the cut one left behind; start each from nothing
        # so a leftover from an earlier collection cannot stand in for this one's.
        shutil.rmtree(self.collected_root, ignore_errors=True)
        self.collected_root.mkdir()
        return self.cluster.collect(_OPERATION)

    def ledger_state(self) -> LedgerState:
        entry = self.ledger.read(_KEY)
        assert entry is not None
        return entry.record.state

    def settle(self, collected: ClusterCollected | ClusterUnknown) -> bool:
        entry = self.ledger.read(_KEY)
        assert entry is not None
        return settle(self.ledger, entry, SlurmJobStatus.COMPLETED, collected)


def _world(base: Path) -> _World:
    """One finished two-stage batch on the executing Fake, recorded submitted in a ledger."""
    connector = FakeConnector(base / "connector")
    drop = _Drop()
    connector.intercept(drop)
    remote = base / "remote"
    remote.mkdir()
    workspace = base / "workspace"
    workspace.mkdir()
    collected_root = base / "collected"
    collected_root.mkdir()
    runner = SlurmJobRunner(
        SlurmConfig(
            name="drop",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        ),
        processes=connector,
        clock=ManualClock(),
    )
    cluster = SlurmCluster(runner, state_root=base / "identity")
    request = SlurmBatchRequest(
        workspace=workspace,
        stages=(
            SlurmBatchStage(
                name="benchmark",
                command=("bash", "-c", _BENCHMARK),
                file_artifacts=(SlurmFileArtifact("score.txt", collected_root / "score.txt"),),
                tree_artifacts=(SlurmTreeArtifact("profile", collected_root / "profile"),),
            ),
            SlurmBatchStage(name="report", command=("printf", "done")),
        ),
    )
    submitted = cluster.submit(request, operation_id=_OPERATION)
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmBatchHandle)
    ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
    ledger.claim(
        LedgerRecord(
            key=_KEY,
            project=PROJECT,
            attempt=0,
            operation_id=_OPERATION,
            owner="run-a",
            created_at=0.0,
            claim_expires_at=900.0,
            state=LedgerState.SUBMITTED,
            job=job_of_handle(submitted.handle),
        )
    )
    ledger.renew(_KEY, LedgerLease(holder="run-a", renewed_at=0.0, expires_at=300.0))
    return _World(cluster, drop, submitted.handle, collected_root, ledger)


def test_a_download_cut_anywhere_settles_nothing_and_a_later_collection_settles_completed(
    tmp_path: Path,
) -> None:
    """Every download of the collection, cut at every point, reads no result at all.

    The executing Fake runs the job once, at sbatch; each collection after that only
    downloads, so every (download, cut point) pair of the collection is tried.
    """
    world = _world(tmp_path)
    job = job_of_handle(world.handle)
    whole = world.collect()
    assert isinstance(whole, ClusterCollected)
    downloads = [Path(str(get["remote_path"])) for get in world.drop.gets]
    # Status file, log, stage result tree, and the passed stage's two artifacts.
    assert len(downloads) == 5

    tried = 0
    for which, remote in enumerate(downloads):
        for landed in _cut_points(remote):
            world.drop.gets.clear()
            world.drop.which, world.drop.landed = which, landed
            collected = world.collect()
            where = f"download {which} ({remote.name}) cut at {landed!r}"
            assert settlement(SlurmJobStatus.COMPLETED, collected, job=job) is None, where
            assert not world.settle(collected), where
            assert world.ledger_state() is LedgerState.SUBMITTED, where
            assert isinstance(collected, ClusterUnknown), where
            assert collected.result is None, where  # nothing read from a partial copy
            tried += 1
    assert tried > len(downloads)

    world.drop.which = None
    again = world.collect()
    assert isinstance(again, ClusterCollected)
    assert isinstance(again.result, SlurmBatchResult)
    assert [stage.exit_code for stage in again.result.stages] == [0, 0]
    assert again.result.stages[0].stdout == "score 42"
    assert (world.collected_root / "score.txt").read_text(encoding="utf-8") == "score 42"
    assert (world.collected_root / "profile" / "b.txt").read_text(encoding="utf-8") == "b"
    assert world.settle(again)
    assert world.ledger_state() is LedgerState.COMPLETED
