"""Every fenced executor pair: no setup creates anything after its release answered released.

The schedules are drawn from the test's sim seed: which deliveries each host runs (the setup,
its redelivery, a late setup under another identity, the release), which of them run
concurrently on one host, and at which durable-write boundary a host dies. Each schedule runs
on simulated threads under its own schedule seed, so the interleaving of concurrent
deliveries varies too, and one seed always replays one run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.fence_cases import FENCE_PAIRS

from vs_runtime.api.core import FenceKind
from vs_runtime.api.fence_contracts import HostRun, run_schedule
from vs_sim.api.testing import Sim, SimBlockingRunner, examples

if TYPE_CHECKING:
    from vs_runtime.api.fence_contracts import DeliveryKind, FencePair, ScheduleReport
    from vs_sim.api import SeededRandom

_KINDS: tuple[DeliveryKind, ...] = ("setup", "setup", "late-setup", "release")
_BOUNDARIES = 16
"""Covers the writes of a setup up to its creation and answer, where a crash matters most."""
_STAGGER = 4
"""At most this many blocking calls before a delivery starts, so one starts inside another."""


def _schedule(random: SeededRandom) -> tuple[HostRun, ...]:
    runs = []
    for _ in range(random.randint(1, 4)):
        deliveries = tuple(random.choice(_KINDS) for _ in range(random.randint(1, 3)))
        crash_at = random.randint(0, _BOUNDARIES - 1) if random.random() < 0.5 else None
        stagger = tuple(random.randint(0, _STAGGER) for _ in deliveries)
        runs.append(HostRun(deliveries, crash_at, stagger))
    if not any("release" in run.deliveries for run in runs):
        runs.insert(random.randint(0, len(runs)), HostRun(("release",)))
    return tuple(runs)


def _run(pair: FencePair, runs: tuple[HostRun, ...], schedule_seed: int) -> ScheduleReport:
    simulation = Sim(seed=schedule_seed, schedule_seed=schedule_seed)
    threads = simulation.threads()

    async def main() -> ScheduleReport:
        async with pair.world() as world:
            return await run_schedule(world, runs, SimBlockingRunner(threads))

    return simulation.run(main(), threads=threads)


def test_every_fence_kind_has_exactly_one_pair() -> None:
    """A new `FenceKind` fails here until its executor pair joins the contract."""
    assert sorted(pair.kind for pair in FENCE_PAIRS) == sorted(FenceKind)


@pytest.mark.parametrize("pair", FENCE_PAIRS, ids=lambda pair: pair.name)
def test_no_setup_creates_anything_after_its_release_answered_released(
    pair: FencePair, sim: Sim
) -> None:
    random = sim.random(pair.name)
    for _ in range(examples(pr=1, nightly=60)):
        runs = _schedule(random)
        schedule_seed = random.getrandbits(32)
        report = _run(pair, runs, schedule_seed)
        assert not report.violations, (
            f"{pair.name}: schedule {runs} under schedule seed {schedule_seed}: {report.violations}"
        )


_TURN_CANCEL = next(pair for pair in FENCE_PAIRS if pair.name == "turn-cancel")


@pytest.mark.parametrize(
    ("runs", "schedule_seed"),
    [
        ((HostRun(("setup", "setup", "release")),), 344698598),
        ((HostRun(("release", "setup", "setup"), crash_at=21),), 2894537353),
    ],
)
def test_a_cancel_racing_its_dispatch_never_answers_released_before_the_provider_call(
    runs: tuple[HostRun, ...], schedule_seed: int
) -> None:
    """The cancel ran between the dispatch's record and its provider call, on the same host.

    It found a dispatch record and no journal row, released nothing real and answered
    CANCELLED and released; the dispatch then called the provider. Now the cancel closes the
    invocation's fence first: the in-flight dispatch is either still inside it (Unknown) or
    stopped at its second entry.
    """
    report = _run(_TURN_CANCEL, runs, schedule_seed)
    assert not report.violations, report.violations


_SCOPE_CLOSE = next(pair for pair in FENCE_PAIRS if pair.name == "scope-close")


@pytest.mark.parametrize(
    ("runs", "schedule_seed"),
    [
        ((HostRun(("setup", "release")),), 0),
        ((HostRun(("release", "setup")),), 2),
        ((HostRun(("setup", "release"), stagger=(1, 4)),), 3222738317),
    ],
)
def test_a_scope_close_racing_a_submission_never_answers_released_before_the_submit(
    runs: tuple[HostRun, ...], schedule_seed: int
) -> None:
    """The close ran between the submission's executor poll and its submit, on the same host.

    The submission had registered its job and polled it unsubmitted; the close cancelled
    the job, polled it unsubmitted too, counted it ended and answered released; the
    submission then submitted it. Now the close closes the scope episode's fence first: a
    submission still inside it keeps the close unreleased, and one that has not re-entered
    by then submits nothing. The third schedule starts the close after the submission
    entered again and before its submit lands: the close must not count the job ended.
    """
    report = _run(_SCOPE_CLOSE, runs, schedule_seed)
    assert not report.violations, report.violations


_JOB_CANCEL = next(pair for pair in FENCE_PAIRS if pair.name == "job-cancel")


@pytest.mark.parametrize(
    ("runs", "schedule_seed"),
    [
        ((HostRun(("release", "setup"), stagger=(3, 0)),), 1026898881),
        ((HostRun(("release",)), HostRun(("setup", "setup"))), 3841107728),
    ],
)
def test_a_job_cancel_never_answers_released_before_its_submission_submits(
    runs: tuple[HostRun, ...], schedule_seed: int
) -> None:
    """A cancel of a job whose submission had not registered, or had not landed, yet.

    The cancel rejected the unregistered job as released, or counted the unsubmitted one as
    ended, and the submission then submitted it. Now the cancel closes the job's fence first:
    a submission it did not see submits nothing, and one still inside keeps it unreleased.
    """
    report = _run(_JOB_CANCEL, runs, schedule_seed)
    assert not report.violations, report.violations
