"""The Slurm executor's evaluations that run through the evaluation ledger (#2757).

``LedgerEvaluations`` holds what an executor does with its run's ledger
attachment (``SlurmLedgerAttachment``) for one evaluation: attach to the
request's job, keep it leased while it runs, record how it ended, and let go
of it when the evaluation is cancelled. The executor keeps its own durable
records and lifecycle; this module reaches the ledger and the cluster only
through the executor's worker threads (``BlockingCalls``), so ``close`` still
waits for every call it started.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from vs_evaluation.api import ExecutorCancellationUnknownError, ExecutorRejectedError
from vs_sandbox.slurm_ledger import Attached, LedgerWithdrawnError
from vs_slurm.api import LedgerError, NotInLedger, Withdrawn, WithdrawUnknown

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.slurm_ledger import AttachOutcome, SlurmLedgerAttachment
    from vs_sim.api import Clock, Sleeper
    from vs_slurm.api import (
        Cluster,
        ClusterCollectOutcome,
        LedgerLabels,
        SlurmBatchRequest,
        SlurmJobStatus,
        WithdrawOutcome,
    )

_LOG = logging.getLogger(__name__)
# How many more times a relinquish whose outcome is unknown is tried before its
# caller hears so; each try is a few ledger and cluster calls.
_RELINQUISH_RETRIES = 3

CLOSE_RELEASE_BOUND_S = 30.0
"""How long an executor's close keeps starting ledger relinquishes, in seconds.

A healthy controller relinquishes every job well within it. On one that does not answer,
each try waits up to a transport call's limit and is retried, with no end but the
retries. Past the bound, a close starts no further relinquish: a job left in the ledger is
detached, as a run that keeps its jobs on a stop leaves it. Its record stays resumable,
its ledger lease lapses, and the next run that opens the ledger reconciles it. A job with
no ledger entry (attached before the ledger was enabled) is still cancelled.

This bounds when relinquishes start, not how long the close takes. Typically the close
ends within the bound plus one transport call: on a controller that does not answer, the
first call that times out ends its try. There is no hard bound, because what is already
under way finishes:

- a relinquish in flight is a whole ledger withdrawal, up to 8 rounds of a read and then a
  mark write, or an inspect, scancel and record;
- after a withdrawal, and for a job with no ledger entry, the executor sends scancel and
  confirms the job ended, up to ``cancel_confirmation_seconds`` worth of inspects;
- tries already queued on the worker threads at the deadline still run.

Each of these calls is bounded by ``transport_timeout_seconds``. In the extreme, a
controller that answers every call just under that limit (110 s by default) keeps one job's
release going for tens of calls, an hour or more. Correctness does not depend on the close
ending: an interrupted withdrawal's mark expires and is undone."""


class BlockingCalls(Protocol):
    """Runs one blocking call on a worker thread the executor waits for on ``close``."""

    async def __call__[**P, R](
        self, function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Return what ``function`` returns, called on a worker thread."""
        ...


class Released(StrEnum):
    """What letting go of a ledger evaluation leaves to its executor."""

    # Nothing: the job is cancelled, kept for other runs, or left to the ledger.
    DONE = "done"
    # The job is gone from the ledger, or never was in it: confirm it ended, or cancel it.
    CANCEL_HERE = "cancel_here"
    # The run is detaching: the job is left running with its record.
    DETACHED = "detached"


@dataclass
class LedgerEvaluations:
    """One executor's ledger evaluations, over its run's attachment.

    ``in_thread`` runs a blocking call; ``finish_in_thread`` runs one that must
    finish even when its awaiter is cancelled. ``poll_interval`` paces the wait
    on another process's submission, up to ``job_timeout_seconds``.
    """

    ledger: SlurmLedgerAttachment
    cluster: Cluster
    clock: Clock
    sleeper: Sleeper
    poll_interval: float
    job_timeout_seconds: int
    in_thread: BlockingCalls
    finish_in_thread: BlockingCalls
    release_until: float | None = field(default=None, init=False)
    """When a close stops starting releases (``give_up_releasing_after``); None: never."""

    def give_up_releasing_after(self, seconds: float) -> None:
        """Start no relinquish ``seconds`` from now; a job left in the ledger detaches."""
        self.release_until = self.clock.now() + seconds

    def _gave_up(self) -> bool:
        return self.release_until is not None and self.clock.now() >= self.release_until

    async def attach(
        self,
        handle_id: str,
        batch: SlurmBatchRequest,
        labels: LedgerLabels,
        save: Callable[[Attached], None],
    ) -> Attached | None:
        """Attach to the ledger's job for this request, submitting it only if it has none.

        While another process holds the request's submission, wait for it, up to
        the job deadline; a claim whose submitter died expires (``claim_seconds``)
        and the wait takes it over. A definite rejection raises like a direct
        submission, and so does an operator's withdrawal of the attempt this
        submission was recording: it ends the evaluation instead of resubmitting.
        ``save`` records the job a cancelled attach still attached to; the
        job attached to is returned, and None once the job deadline passed.
        """
        ledger = self.ledger
        deadline = self.clock.now() + self.job_timeout_seconds
        key = await self.in_thread(ledger.key, batch)
        await self.in_thread(ledger.bind, handle_id, key)
        while True:
            # A sibling evaluation's withdrawal of this request runs wholly before
            # this step or wholly after it (``SlurmLedgerAttachment.guard``).
            async with ledger.guard(key):
                ledger.use(key, handle_id)
                outcome = await self._attach_step(key, batch, labels, save)
            if isinstance(outcome, Attached):
                return outcome
            remaining = deadline - self.clock.now()
            if remaining <= 0:
                return None
            _LOG.info("evaluation %s waits on the ledger: %s", handle_id, outcome.reason)
            await self.sleeper.sleep(min(self.poll_interval, remaining))

    async def _attach_step(
        self,
        key: str,
        batch: SlurmBatchRequest,
        labels: LedgerLabels,
        save: Callable[[Attached], None],
    ) -> AttachOutcome:
        """One attach call, which finishes, and records its job, even when cancelled."""
        step = asyncio.ensure_future(
            self.in_thread(self.ledger.attach, self.cluster, batch, labels=labels, key=key)
        )
        # A step may stage and submit the job for a long time: renewing this run's
        # lease meanwhile keeps its claim from being taken over (``claim_expired``).
        keeper = asyncio.create_task(self._keep_leased(key))
        try:
            return await asyncio.shield(step)
        except asyncio.CancelledError:
            # A submission this step started finishes and is recorded, so a
            # restart reattaches to its job instead of submitting another.
            with contextlib.suppress(Exception):
                attached = await step
                if isinstance(attached, Attached):
                    save(attached)
            raise
        except LedgerWithdrawnError as error:
            raise ExecutorRejectedError(str(error)) from error
        finally:
            keeper.cancel()
            # Not ``suppress(CancelledError)``: that would also swallow a cancel of
            # this task that lands while it waits for the keeper to end.
            await asyncio.gather(keeper, return_exceptions=True)

    def resumed(self, handle_id: str, key: str) -> None:
        """Count an evaluation this process resumed as a user of its request ``key``."""
        self.ledger.use(key, handle_id)

    async def _keep_leased(self, key: str) -> None:
        """Renew this run's lease on ``key`` every third of a lease period, until cancelled.

        A renewal that fails is logged and retried at the next period: the lease
        outlasts several missed renewals.
        """
        period = self.ledger.policy.lease_seconds / 3
        while True:
            await self.sleeper.sleep(period)
            try:
                await self.in_thread(self.ledger.renew, key)
            except LedgerError:
                _LOG.exception("could not renew the ledger lease on request %s", key[:12])

    async def heartbeat(self, handle_id: str, key: str, operation_id: str) -> bool:
        """Keep a ledger evaluation leased; return False once it was withdrawn.

        A ledger the run cannot reach does not fail the evaluation: the lease
        outlasts several missed renewals, and the job's own state still decides.
        This check never publishes lifecycle state: a live owner must first settle
        its streams and ledger, while an ownerless observer has no such debt.
        """
        try:
            async with self.ledger.guard(key):
                await self.in_thread(self.ledger.heartbeat, self.cluster, key, operation_id)
        except LedgerWithdrawnError:
            _LOG.warning("evaluation %s was withdrawn from the evaluation ledger", handle_id)
            return False
        except LedgerError:
            _LOG.exception("could not renew the ledger lease of evaluation %s", handle_id)
        return True

    async def settle(
        self,
        handle_id: str,
        key: str,
        operation_id: str,
        status: SlurmJobStatus,
        collected: ClusterCollectOutcome | None,
    ) -> None:
        """Record a ledger job's end, so later asks reuse or replace it without asking Slurm.

        It runs after collection: only results this run collected, with every
        stage's own result, make the attempt reusable. Best effort: an
        unreachable ledger leaves the attempt to the next asker, who inspects
        the job itself.
        """
        try:
            await self.in_thread(self.ledger.settle, key, operation_id, status, collected)
        except LedgerError:
            _LOG.exception("could not record the end of ledger evaluation %s", handle_id)

    async def release(
        self, handle_id: str, known: str | None, *, attached: bool, detaching: bool
    ) -> Released:
        """Let go of a ledger job.

        This run stops wanting the request (``SlurmLedgerAttachment.relinquish``):
        unless another run wants it, the ledger cancels its job, which is then
        confirmed ended by the executor like any other. When the ledger keeps
        jobs on a stop, this run only drops its lease, and a stop (``detaching``)
        leaves even that. ``known`` is the request of the job the evaluation
        attached to (``attached``), as its executor recorded it; a job attached
        to before the operator enabled the ledger has none, and is cancelled by
        the executor as before. A close that gave up releasing
        (``give_up_releasing_after``) starts no further relinquish and detaches
        the job, which its ledger entry leaves to a later run; it still cancels a
        job that has no entry.
        """
        if detaching:
            return Released.DETACHED
        if attached and known is None:
            # Even past a close's bound: with no ledger entry, no sweep would withdraw it.
            return Released.CANCEL_HERE
        if self._gave_up():
            # Nor wait out another evaluation's guard on the request into a try.
            return Released.DETACHED
        return await self._release_from_ledger(handle_id, known, attached=attached)

    async def _release_from_ledger(
        self, handle_id: str, known: str | None, *, attached: bool
    ) -> Released:
        """Stop using this evaluation's request; relinquish it if no other run wants it."""
        ledger = self.ledger
        key = known if known is not None else ledger.request_of(handle_id)
        if key is None:
            return Released.DONE  # it never claimed a request
        async with ledger.guard(key):
            sole = ledger.last_user(handle_id, key) is not None
            outcome: WithdrawOutcome | None = None
            if sole and ledger.policy.keep_jobs_on_stop:
                # Without a job attached, a submission this run started is held by its lease.
                if known is not None:
                    await self.finish_in_thread(ledger.release, known)
            elif sole:
                outcome = await self._relinquish(key)
                if outcome is None:
                    return Released.DETACHED
                if isinstance(outcome, WithdrawUnknown):
                    # Still used: a later cancel, or the stop, withdraws it again.
                    raise ExecutorCancellationUnknownError(handle_id)
            ledger.stop_using(handle_id)
        if attached and isinstance(outcome, Withdrawn | NotInLedger):
            return Released.CANCEL_HERE
        return Released.DONE

    async def _relinquish(self, key: str) -> WithdrawOutcome | None:
        """Relinquish ``key``, trying again at once while its outcome is unknown.

        A scancel or ledger call lost over a broken connection undoes the
        withdrawal's mark, or leaves it to expire (``withdraw``), so trying
        again is safe; a stop that gave
        up after one lost call would leave the job queued for nobody. The tries
        do not pause: each is several remote calls, and the caller holds the
        request's guard, which a pause would hold too. None: a close gave up
        releasing (``give_up_releasing_after``) before the outcome was known.
        """
        unknown: WithdrawUnknown | None = None
        for _ in range(1 + _RELINQUISH_RETRIES):
            if self._gave_up():
                _LOG.warning("left ledger request %s for the next run to reconcile", key[:12])
                return None
            if unknown is not None:
                _LOG.warning("relinquishing ledger request %s again: %s", key[:12], unknown.reason)
            outcome = await self.finish_in_thread(self.ledger.relinquish, self.cluster, key)
            if not isinstance(outcome, WithdrawUnknown):
                return outcome
            unknown = outcome
        return unknown

    async def let_go_of_ended(self, handle_id: str, known: str | None) -> None:
        """Stop using the request of an evaluation that ended, relinquishing it if last.

        Its job ended, or never started: the relinquish only drops this run's
        lease. One whose outcome is unknown keeps the use, for a later stop,
        but its lease is already dropped: unless another run wants the request,
        the grace sweep withdraws it.
        """
        try:
            await self.release(handle_id, known, attached=known is not None, detaching=False)
        except ExecutorCancellationUnknownError:
            _LOG.warning("could not let go of the ledger request of evaluation %s", handle_id)


__all__ = ["CLOSE_RELEASE_BOUND_S", "BlockingCalls", "LedgerEvaluations", "Released"]
