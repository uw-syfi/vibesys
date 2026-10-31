"""The semantic Slurm executor names the ledger key of a request without submitting it.

A process that holds only a request, and not the executor that attached it, finds the job an
earlier process submitted by computing the key the ledger holds it under.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_sandbox.api.slurm import SlurmExecutionPolicy, SlurmLedgerAttachment
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock, run_virtual
from vs_slurm.api import (
    FakeEvaluationLedger,
    LedgerPolicy,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
)

from .test_semantic_slurm_ledger_close import _request

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.slurm_evaluation_stack import Stack

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="test")
)


def _attachment(ledger: FakeEvaluationLedger, clock: VirtualClock) -> SlurmLedgerAttachment:
    return SlurmLedgerAttachment(
        ledger=ledger,
        config=CONFIG,
        project=PROJECT,
        holder="run-1",
        policy=LedgerPolicy(),
        clock=clock,
    )


async def _stack(
    root: Path, clock: VirtualClock, ledger: FakeEvaluationLedger, policy: SlurmExecutionPolicy
) -> Stack:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)
    return await build_stack(
        root, cluster, sleeper=clock, ledger=_attachment(ledger, clock), policy=policy
    )


def test_the_key_is_the_one_the_ledger_holds_the_attached_job_under(tmp_path: Path) -> None:
    clock = VirtualClock(1000.0)
    ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())

    async def main() -> None:
        stack = await _stack(tmp_path, clock, ledger, SlurmExecutionPolicy())
        try:
            request = _request(stack.snapshot)
            await stack.executor.submit(request, handle_id="eval-a")
            await clock.sleep(30.0)  # the evaluation attaches; the job is queued
            (held,) = ledger.list_keys()
            entry = ledger.read(held)
            assert entry is not None
            opened = len(stack.workspaces.candidates)
            assert await stack.executor.ledger_key(request) == held
            # Another handle of the same content has the same key: the key names no handle.
            assert await stack.executor.ledger_key(request.model_copy(update={"key": "x"})) == held
            assert stack.cluster.submissions == [entry.record.operation_id]
            keyed = stack.workspaces.candidates[opened:]
            assert len(keyed) == 2
            assert all(candidate.discarded for candidate in keyed)
        finally:
            await stack.executor.close()

    run_virtual(clock, main(), driver=SimThreads(clock, schedule_seed=None))


def test_a_new_process_names_the_key_of_a_job_it_never_attached(tmp_path: Path) -> None:
    clock = VirtualClock(1000.0)
    ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())

    async def main() -> None:
        first = await _stack(tmp_path / "first", clock, ledger, SlurmExecutionPolicy())
        second = await _stack(tmp_path / "second", clock, ledger, SlurmExecutionPolicy())
        try:
            request = _request(first.snapshot)
            await first.executor.submit(request, handle_id="eval-a")
            await clock.sleep(30.0)
            (held,) = ledger.list_keys()
            assert await second.executor.ledger_key(_request(second.snapshot)) == held
            assert second.cluster.submissions == []
        finally:
            await second.executor.close()
            await first.executor.close()

    run_virtual(clock, main(), driver=SimThreads(clock, schedule_seed=None))


def test_the_key_follows_the_stage_spec(tmp_path: Path) -> None:
    clock = VirtualClock(1000.0)
    ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())

    async def main() -> None:
        plain = await _stack(tmp_path / "plain", clock, ledger, SlurmExecutionPolicy())
        flagged = await _stack(
            tmp_path / "flagged",
            clock,
            ledger,
            SlurmExecutionPolicy(accuracy_arguments=("--fast",)),
        )
        try:
            same = await plain.executor.ledger_key(_request(plain.snapshot))
            other = await flagged.executor.ledger_key(_request(flagged.snapshot))
            assert same != other
        finally:
            await flagged.executor.close()
            await plain.executor.close()

    run_virtual(clock, main(), driver=SimThreads(clock, schedule_seed=None))


def test_a_stack_without_a_ledger_has_no_key(tmp_path: Path) -> None:
    clock = VirtualClock(1000.0)

    async def main() -> None:
        stack = await build_stack(tmp_path, ScenarioCluster(), sleeper=clock)
        try:
            with pytest.raises(ValueError, match="no evaluation ledger"):
                await stack.executor.ledger_key(_request(stack.snapshot))
        finally:
            await stack.executor.close()

    run_virtual(clock, main(), driver=SimThreads(clock, schedule_seed=None))
