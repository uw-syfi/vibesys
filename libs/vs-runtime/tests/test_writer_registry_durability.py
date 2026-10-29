"""Atomic grant, closure and proof writes grant no authority after a lost reply."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

import pytest

from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import (
    AtomicWriteStream,
    FakeStateStore,
    LocalAtomicWriteEffects,
    Project,
    StateNamespace,
)
from vs_proto.api import replay
from vs_runtime.api.replay import UnsupportedWriterReconciler, WriterRegistry
from vs_sim.api import NotReleasedError
from vs_sim.api.testing import RefusingThreads, SimThreads

from .test_writer_reconciliation import BACKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_sim.api import Lock


@dataclass
class CrossingStream:
    stream: AtomicWriteStream
    gate: FaultGate

    def write(self, contents: bytes, /) -> int | None:
        return self.gate.around(
            Boundary.DURABLE_WRITE, "write", partial(self.stream.write, contents)
        )

    def flush(self) -> None:
        self.gate.around(Boundary.DURABLE_WRITE, "flush", self.stream.flush)

    def fileno(self) -> int:
        return self.stream.fileno()

    @property
    def closed(self) -> bool:
        return self.stream.closed


class CrossingAtomic(LocalAtomicWriteEffects):
    def __init__(self, gate: FaultGate) -> None:
        self.gate = gate

    @contextmanager
    def temporary(self, destination: Path) -> Iterator[tuple[Path, AtomicWriteStream]]:
        with super().temporary(destination) as (path, stream):
            yield path, CrossingStream(stream, self.gate)

    def sync_file(self, stream: AtomicWriteStream) -> None:
        self.gate.around(Boundary.DURABLE_WRITE, "fsync", partial(super().sync_file, stream))

    def replace(self, temporary: Path, destination: Path) -> None:
        self.gate.around(
            Boundary.DURABLE_WRITE, "replace", partial(super().replace, temporary, destination)
        )

    def sync_directory(self, directory: Path) -> None:
        self.gate.around(
            Boundary.DURABLE_WRITE, "directory", partial(super().sync_directory, directory)
        )


@dataclass
class AtomicNamespace:
    namespace: StateNamespace
    effects: CrossingAtomic

    def write_bytes(self, relative_path: str, contents: bytes) -> None:
        self.namespace.write_bytes(relative_path, contents, effects=self.effects)

    def __getattr__(self, name: str) -> object:
        return getattr(self.namespace, name)


@pytest.mark.parametrize("step", ["grant", "close", "proof", "advance"])
@pytest.mark.parametrize("boundary", ["write", "flush", "fsync", "replace", "directory"])
def test_writer_registry_atomic_crashes_reload_complete_old_or_new_evidence(
    tmp_path: Path, step: str, boundary: str
) -> None:
    namespace = Project.open(tmp_path).state.state_store_namespace("writers")
    gate = FaultGate(FaultPlan(seed=0))
    ledger = WriterRegistry(
        cast("StateNamespace", AtomicNamespace(namespace, CrossingAtomic(gate))),
        BACKING,
        SimThreads(),
    )
    ledger.create()
    writer = replay.WriterIdentity(
        run_id="run-a", host_incarnation="host-a/1", physical_id="child-a", domain_id="domain-a"
    )
    if step != "grant":
        ledger.grant(writer)
    manifest = ledger.close() if step in {"proof", "advance"} else None
    result = (
        None
        if manifest is None
        else replay.WriterReconciliation(
            manifest=manifest,
            status=replay.WriterReconciliationStatus.PROVEN_STOPPED,
            proof=replay.WriterTerminationProof(
                manifest=manifest, absent_domains=[writer.domain_id]
            ),
        )
    )
    backing = deepcopy(BACKING)
    if step == "advance":
        assert result is not None
        ledger.record(result)
        backing.generation += 1
        ledger = WriterRegistry(
            cast("StateNamespace", AtomicNamespace(namespace, CrossingAtomic(gate))),
            backing,
            SimThreads(),
        )

    def perform(registry: WriterRegistry) -> None:
        if step == "grant":
            registry.grant(writer)
        elif step == "close":
            registry.close()
        elif step == "advance":
            registry.create()
        else:
            assert result is not None
            registry.record(result)

    gate.plan = FaultPlan(
        seed=0,
        rules=(
            FaultRule(
                boundary=Boundary.DURABLE_WRITE,
                target=boundary,
                at=1 + sum(call.target == boundary for call in gate.calls),
                fault=HostFault.CRASH_AFTER,
            ),
        ),
    )
    with pytest.raises(HostCrashError, match=boundary):
        perform(ledger)
    reopened = WriterRegistry(namespace, backing, SimThreads())
    perform(reopened)
    perform(reopened)
    assert reopened.close().writers == [writer]
    if step == "proof":
        assert reopened.require_proof().manifest == manifest


class FailingRelease:
    """A reentrant lock whose next release fails once ``fail`` is armed, still holding it."""

    def __init__(self, lock: Lock) -> None:
        self.lock, self.fail = lock, False

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:  # noqa: FBT001, FBT002  # LW-994812 [FBT001, FBT002]; the signature of the ``Lock`` protocol this Fake implements.
        # > ReceiptStore calls ``acquire(blocking=...)`` through ``Threads.rlock``; keyword-only
        # > or renamed parameters would no longer satisfy the protocol it is injected through.
        return self.lock.acquire(blocking, timeout)

    def release(self) -> None:
        if self.fail:
            self.fail = False
            problem = "lock release failed"
            raise OSError(problem)
        self.lock.release()

    def __enter__(self) -> object:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


class FailingReleaseThreads(RefusingThreads):
    """Threads whose reentrant locks are ``FailingRelease`` locks the test can arm."""

    def __init__(self) -> None:
        super().__init__(SimThreads(), {})
        self.locks: list[FailingRelease] = []
        self.armed = 0

    def arm(self) -> None:
        """Fail the next release of every lock; counts how often a reuse scope armed it."""
        self.armed += 1
        for lock in self.locks:
            lock.fail = True

    def rlock(self) -> Lock:
        lock = FailingRelease(super().rlock())
        self.locks.append(lock)
        return lock


@pytest.mark.regression
def test_a_failed_reuse_release_is_retried_and_refuses_before_exclusion_is_retaken(
    tmp_path: Path,
) -> None:
    threads = FailingReleaseThreads()
    namespace = Project.open(tmp_path).state.state_store_namespace("backing")
    ledger = WriterRegistry(namespace, BACKING, threads)
    ledger.create()
    ledger.record(UnsupportedWriterReconciler("host").reconcile_writers(ledger.close()))
    fence = FakeStateStore().acquire("host-a", 0, 10)
    assert fence is not None
    # The scope's exit is the next release: it fails and leaves the exclusion held.
    with (
        pytest.raises(OSError, match="lock release failed"),
        ledger.reuse(run_id="run-a", fence=fence),
    ):
        threads.arm()
    # The release that failed is retried first, and it refuses rather than reporting released.
    with pytest.raises(NotReleasedError), ledger.reuse(run_id="run-a", fence=fence):
        threads.arm()
    assert threads.armed == 1
