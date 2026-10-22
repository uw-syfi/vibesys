"""Imported accounting publishes once through the single run.log writer, across crashes."""

from contextlib import suppress
from fractions import Fraction
from functools import partial
from pathlib import Path

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import AccountingAmount, ReplayError
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import Project
from vs_proto.api import replay
from vs_runlog.api import FileRunLogWriter, OsFileSync, open_run_log
from vs_runtime.api.replay import ReplayCommitError, RunLogReplayStore
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


def _session(project: Project, request: str, gate: FaultGate | None = None) -> AccountingAmount:
    path = project.run_log_path_for(project.root, "run-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    real = OsFileSync()

    def sync(descriptor: int, /) -> None:
        if gate is None:
            real(descriptor)
        else:
            gate.around(Boundary.DURABLE_WRITE, "fsync", partial(real, descriptor))

    threads = SimThreads()
    with FileRunLogWriter(path, clock=threads.clock, threads=threads, sync=sync) as writer:
        shell = runtime(RunLogReplayStore(writer, project.state_store("run-a")))
        shell.start(launch(request), now=0, at=Timestamp())
        try:
            shell.accounting_evidence(IMPORTED, now=0, at=Timestamp())
            shell.accounting_evidence(LATE, now=0, at=Timestamp())
            accounting = shell.accounting.resources
        finally:
            shell.close(now=0)
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


def test_repeated_import_and_recovery_publish_and_charge_once(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    gate = FaultGate(FaultPlan(seed=0))
    assert _session(Project.open(baseline), "first", gate) == AccountingAmount(Fraction(110))
    boundaries = len(gate.calls)
    for offset in swept(boundaries, sample=4):
        root = tmp_path / f"case-{offset}"
        root.mkdir()
        project = Project.open(root)
        gate = FaultGate(
            FaultPlan(
                seed=0,
                rules=(
                    FaultRule(
                        boundary=Boundary.DURABLE_WRITE,
                        target="fsync",
                        at=offset + 1,
                        fault=HostFault.CRASH_AFTER,
                    ),
                ),
            )
        )
        with suppress(HostCrashError, ReplayCommitError, OSError):
            _session(project, "first", gate)
        gate.heal()
        assert _session(project, "resume", gate) == AccountingAmount(Fraction(110))
        assert _session(project, "again", gate) == AccountingAmount(Fraction(110))
        assert _publications(project) == 1


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
