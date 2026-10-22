"""Imported accounting publishes once through the single run.log writer, across crashes."""

from contextlib import suppress
from fractions import Fraction
from functools import partial
from pathlib import Path

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import AccountingAmount, ReplayError
from vs_faults.api import (
    Boundary,
    Crossing,
    FaultGate,
    FaultPlan,
    FaultRule,
    HostCrashError,
    HostFault,
    InjectedIOError,
)
from vs_project.api import (
    LocalAtomicWriteEffects,
    Project,
    StoredEnvelope,
    Unknown,
)
from vs_proto.api import ReplayStore as ReplayRecords
from vs_proto.api import replay
from vs_runlog.api import FileRunLogWriter, OsFileSync, open_run_log
from vs_runtime.api.replay import (
    REPLAY_STORE_SCHEMA_VERSION,
    ReplayCommitError,
    RunLogReplayStore,
)
from vs_sim.api.testing import SimThreads, swept

from .test_replay_store_contract import launch, runtime

Dimension = replay.AccountingDimension


def _count(value: int | None) -> replay.AccountingQuantity:
    return replay.AccountingQuantity(
        value=Oneof("unknown", value=True) if value is None else Oneof("count", value)
    )


def _occurrence(identity: str) -> replay.AccountingOccurrence:
    return replay.AccountingOccurrence(kind=replay.AccountingOccurrenceKind.INVOCATION, id=identity)


IMPORTED = replay.AccountingEvidence(
    import_id="import-a",
    coverage=[
        replay.AccountingCoverage(
            id="agent:input_tokens",
            dimension=Dimension.INPUT_TOKENS,
            total=_count(100),
            cells=[
                replay.AccountingCell(occurrence=_occurrence("invocation-a"), baseline=_count(30))
            ],
        )
    ],
)
LATE = replay.AccountingEvidence(
    observations=[
        replay.AccountingObservation(
            occurrence=_occurrence("invocation-a"),
            dimension=Dimension.INPUT_TOKENS,
            revision=1,
            charged=_count(40),
        )
    ]
)


class _GatedStoreEffects(LocalAtomicWriteEffects):
    """The state store's real publications, crossing the shared fault gate by file name.

    A rename publishes each store file; a fault in the sync before it publishes nothing,
    exactly like a fault before the rename, so the rename is the one crash point.
    """

    def __init__(self, gate: FaultGate) -> None:
        self._gate = gate

    def replace(self, temporary: Path, destination: Path) -> None:
        replace = super().replace
        self._gate.around(
            Boundary.DURABLE_WRITE,
            f"publish-{destination.stem}",
            partial(replace, temporary, destination),
        )


def _session(
    project: Project, request: str, gate: FaultGate | None = None, *, now: float = 0
) -> AccountingAmount:
    path = project.run_log_path_for(project.root, "run-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    real = OsFileSync()

    def sync(descriptor: int, /) -> None:
        if gate is None:
            real(descriptor)
        else:
            gate.around(Boundary.DURABLE_WRITE, "fsync", partial(real, descriptor))

    store = (
        project.state_store("run-a")
        if gate is None
        else project.state_store("run-a", effects=_GatedStoreEffects(gate))
    )
    threads = SimThreads()
    with FileRunLogWriter(path, clock=threads.clock, threads=threads, sync=sync) as writer:
        shell = runtime(RunLogReplayStore(writer, store))
        shell.start(launch(request), now=now, at=Timestamp())
        try:
            shell.accounting_evidence(IMPORTED, now=now, at=Timestamp())
            shell.accounting_evidence(LATE, now=now, at=Timestamp())
            accounting = shell.accounting.resources
        finally:
            shell.close(now=now)
    assert accounting.epoch_reported.input_tokens == 0
    return accounting.lifetime[Dimension.INPUT_TOKENS]


def _publications(project: Project) -> int:
    published = 0
    with open_run_log(project.run_log_path_for(project.root, "run-a")) as reader:
        for record in reader:
            detail = (
                record.body.value.detail
                if record.body is not None and record.body.field == "notice"
                else None
            )
            if detail is not None and detail.type_url.endswith("AccountingEvidence"):
                published += replay.AccountingEvidence.from_binary(detail.value).has_field(
                    "import_id"
                )
    return published


@pytest.fixture(scope="module")
def crash_points(tmp_path_factory: pytest.TempPathFactory) -> list[Crossing]:
    """Sampled durable writes of a fault-free run, plus every record-store publication.

    The format fence is the record store's only content; it precedes run.log's first record.
    """
    gate = FaultGate(FaultPlan(seed=0))
    project = Project.open(tmp_path_factory.mktemp("baseline"))
    assert _session(project, "first", gate) == AccountingAmount(Fraction(110))
    fence = {index for index, call in enumerate(gate.calls) if call.target == "publish-store"}
    sampled = {*swept(len(gate.calls), sample=4), *fence}
    return [gate.calls[index] for index in sorted(sampled)]


@pytest.mark.parametrize("fault", list(HostFault))
def test_repeated_import_and_recovery_publish_and_charge_once(
    tmp_path: Path, fault: HostFault, crash_points: list[Crossing]
) -> None:
    """A crash or failed sync at any durable write, including the fence, charges once."""
    for case, crossing in enumerate(crash_points):
        root = tmp_path / f"case-{case}"
        root.mkdir()
        project = Project.open(root)
        rule = FaultRule(
            boundary=crossing.boundary, target=crossing.target, at=crossing.ordinal, fault=fault
        )
        gate = FaultGate(FaultPlan(seed=0, rules=(rule,)))
        with suppress(HostCrashError, ReplayCommitError, OSError):
            _session(project, "first", gate)
        gate.heal()
        # A crashed host's lease outlives it; the next host resumes after the lease expires.
        later = 3600
        assert _session(project, "resume", gate, now=later) == AccountingAmount(Fraction(110))
        assert _publications(project) == 1


def test_a_failed_log_sync_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "written").mkdir()
    (tmp_path / "fresh").mkdir()
    written = Project.open(tmp_path / "written")
    _session(written, "first")
    with open_run_log(written.run_log_path_for(written.root, "run-a")) as reader:
        first = next(iter(reader))
    project = Project.open(tmp_path / "fresh")
    path = project.run_log_path_for(project.root, "run-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    # The fresh log syncs twice as it opens; the third sync makes the first record durable.
    rule = FaultRule(
        boundary=Boundary.DURABLE_WRITE, target="fsync", at=3, fault=HostFault.IO_ERROR
    )
    gate = FaultGate(FaultPlan(seed=0, rules=(rule,)))
    real = OsFileSync()
    threads = SimThreads()
    writer = FileRunLogWriter(
        path,
        clock=threads.clock,
        threads=threads,
        sync=lambda descriptor: gate.around(
            Boundary.DURABLE_WRITE, "fsync", partial(real, descriptor)
        ),
    )
    try:
        store = RunLogReplayStore(writer, project.state_store("run-a"))
        fence = store.acquire("host-a", 0, 30)
        assert fence is not None
        envelope = StoredEnvelope(
            revision=0,
            schema_version=REPLAY_STORE_SCHEMA_VERSION,
            payload=ReplayRecords(records=[first]).to_binary(),
        )
        outcome = store.commit(None, envelope, fence, 0)
    finally:
        # The writer keeps its failure and raises it again as it closes.
        with pytest.raises(InjectedIOError):
            writer.close()
    assert outcome == Unknown(revision=0)


def test_a_contradictory_republication_commits_nothing(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    _session(project, "first")
    changed = replay.AccountingEvidence.from_binary(IMPORTED.to_binary())
    changed.coverage[0].total = _count(101)
    path = project.run_log_path_for(project.root, "run-a")
    threads = SimThreads()
    with FileRunLogWriter(path, clock=threads.clock, threads=threads) as writer:
        shell = runtime(RunLogReplayStore(writer, project.state_store("run-a")))
        shell.start(launch("resume"), now=0, at=Timestamp())
        with pytest.raises(ReplayError, match="republished"):
            shell.accounting_evidence(changed, now=0, at=Timestamp())
        shell.close(now=0)
    assert _publications(project) == 1
