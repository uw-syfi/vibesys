"""``AdoptionStepRunner`` passes the StepRunner contract, and an adoption repeats no publication."""

from __future__ import annotations

import asyncio
import itertools
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_core.api import (
    AdoptRevision,
    ObservationStatus,
    RequestId,
    RetainedCandidate,
    RunId,
    Scope,
    SettlementId,
    TrustedBaseline,
    VerifyAdoption,
)
from vs_core.api.recovery import Cancelled, CancelReason, Finished
from vs_project.api import Project
from vs_runtime.api import RuntimeContractError
from vs_runtime.api.adoption_step_runner import AdoptionStepRunner
from vs_runtime.api.core import KeyedDocuments, ObservationFactory
from vs_runtime.api.operation_fixtures import revision
from vs_runtime.api.step_runner import CancelSignal, Cleared, ClearUnavailableError
from vs_runtime.api.testing import (
    FakeEvidenceLedger,
    FakeWorkspace,
    FakeWorkspaces,
    StepRunnerContract,
    StepRunnerUnderTest,
)
from vs_sim.api.testing import HANG_GUARD_S, SimThreads, VirtualClock, run_virtual, yield_until

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_core.api import AdoptionPublication, Request

BASE = "a" * 40
GOOD = "b" * 40
HANGS = "c" * 40
STRANGER = "d" * 40
SCOPE = Scope(owner=RunId(root="run"), generation=0)
DEADLINE = 600.0


@dataclass
class World:
    """What the workspaces were asked: publications made, and publications still going."""

    publishing: int = 0
    published: list[str] = field(default_factory=list)
    unreachable: bool = False
    refuses: bool = False


class FlakyRoot(FakeWorkspace):
    """The run's root, which cannot be read while the world is unreachable.

    ``STRANGER`` exists in the repository without being retained by the run.
    """

    def __init__(self, world: World, path: Path) -> None:
        super().__init__(path=path, revision=BASE, known_revisions={BASE, GOOD, HANGS})
        self.add_dangling_revision(STRANGER)
        self.world = world

    async def has_revision(self, revision: str) -> bool:
        if self.world.unreachable:
            message = "repository unreachable"
            raise OSError(message)
        return await super().has_revision(revision)


class PublishingWorkspaces(FakeWorkspaces):
    """Fake workspaces whose publication of ``HANGS`` never ends by itself."""

    def __init__(self, root: FlakyRoot, world: World) -> None:
        super().__init__(root)
        self.world = world

    async def adopt(
        self, revision: str, *, message: str = "vibesys: adopt revision"
    ) -> AdoptionPublication:
        if self.world.refuses:
            detail = "publication refused"
            raise RuntimeContractError(detail)
        self.world.publishing += 1
        try:
            if revision == HANGS:
                await asyncio.get_running_loop().create_future()
            publication = await super().adopt(revision, message=message)
        finally:
            self.world.publishing -= 1
        self.world.published.append(publication.commit)
        return publication


@dataclass
class Harness:
    world: World
    workspaces: PublishingWorkspaces
    store: KeyedDocuments
    clock: VirtualClock

    def runner(self, *, publishes: bool = True) -> AdoptionStepRunner:
        return AdoptionStepRunner(
            self.workspaces,
            FakeEvidenceLedger(),
            ObservationFactory(self.store),
            clock=self.clock,
            publishes=publishes,
        )


def harness_in(root: Path) -> Harness:
    root.mkdir(parents=True, exist_ok=True)
    namespace = Project.open(root).state.state_store_namespace("run")
    world = World()
    workspaces = PublishingWorkspaces(FlakyRoot(world, root / "workspace"), world)
    return Harness(world, workspaces, KeyedDocuments(namespace, SimThreads()), VirtualClock())


def adopt(tag: RequestId, commit: str) -> AdoptRevision:
    selection = RetainedCandidate(
        settlement_id=SettlementId(root="settled"), revision=revision(commit)
    )
    return AdoptRevision(request_id=tag, scope=SCOPE, selection=selection)


def tag_named(name: str) -> RequestId:
    return RequestId(root=name)


async def retain(built: Harness, *commits: str) -> None:
    for commit in commits:
        await built.workspaces.root.retain(commit, label=f"keep-{commit[:4]}")


class TestAdoptionStepRunner(StepRunnerContract):
    @pytest.fixture(autouse=True)
    def _worlds(self) -> Iterator[None]:
        with tempfile.TemporaryDirectory() as raw:
            self._root = Path(raw)
            self._count = itertools.count()
            yield

    def step_runner_under_test(self) -> StepRunnerUnderTest:
        built = harness_in(self._root / f"world-{next(self._count)}")
        run_virtual(built.clock, retain(built, GOOD, HANGS))
        runner = built.runner()

        async def left_running(tag: RequestId) -> int:
            del tag  # one tag per world
            return built.world.publishing

        @contextmanager
        def unreachable() -> Iterator[None]:
            built.world.unreachable = True
            try:
                yield
            finally:
                built.world.unreachable = False

        return StepRunnerUnderTest(
            runner=runner,
            run=lambda main: run_virtual(built.clock, main),
            finishing_step=lambda tag: adopt(tag, GOOD),
            running_step=lambda tag: adopt(tag, HANGS),
            left_running=left_running,
            unreachable=unreachable,
        )


def test_an_adoption_published_twice_is_the_same_publication(tmp_path: Path) -> None:
    built = harness_in(tmp_path)
    tag = tag_named("adopt")

    async def main() -> None:
        await retain(built, GOOD)
        first = await built.runner().run(adopt(tag, GOOD), tag, 1, DEADLINE, CancelSignal())
        assert await built.runner().clear(adopt(tag, GOOD), tag) == Cleared()
        again = await built.runner().run(adopt(tag, GOOD), tag, 2, DEADLINE, CancelSignal())
        assert isinstance(first, Finished)
        assert isinstance(again, Finished)
        assert first.observation.status is ObservationStatus.SUCCEEDED
        assert again.observation.status is ObservationStatus.SUCCEEDED
        assert len(set(built.world.published)) == 1

    run_virtual(built.clock, main())


@pytest.mark.parametrize("verify", [False, True])
def test_the_report_of_a_succeeded_adoption_says_where_it_published_and_what_it_did_to_the_checkout(
    tmp_path: Path, *, verify: bool
) -> None:
    """Core folds the report alone, so what only the report can carry must be on it."""
    built = harness_in(tmp_path)
    tag = tag_named("report")

    async def main() -> None:
        await retain(built, GOOD)
        selection = adopt(tag, GOOD)
        step = (
            VerifyAdoption(request_id=tag, scope=SCOPE, selection=selection.selection)
            if verify
            else selection
        )
        done = await built.runner().run(step, tag, 1, DEADLINE, CancelSignal())
        assert isinstance(done, Finished)
        assert done.report.publication is not None
        assert done.report.publication.commit == built.world.published[-1]
        assert (done.report.checkout is None) is verify

    run_virtual(built.clock, main())


def test_the_report_of_an_adoption_that_published_nothing_carries_no_publication(
    tmp_path: Path,
) -> None:
    built = harness_in(tmp_path)
    tag = tag_named("refused")

    async def main() -> None:
        refused = await built.runner().run(adopt(tag, STRANGER), tag, 1, DEADLINE, CancelSignal())
        kept = await built.runner(publishes=False).run(
            adopt(tag, BASE), tag, 1, DEADLINE, CancelSignal()
        )
        for done in (refused, kept):
            assert isinstance(done, Finished)
            assert (done.report.publication, done.report.checkout) == (None, None)

    run_virtual(built.clock, main())


# What may be adopted: a retained revision of the run, or the trusted baseline.

CASES = {
    ("retained", BASE): ObservationStatus.SUCCEEDED,
    ("retained", GOOD): ObservationStatus.SUCCEEDED,
    ("retained", STRANGER): ObservationStatus.REJECTED,
    ("retained", "e" * 40): ObservationStatus.REJECTED,
    ("baseline", BASE): ObservationStatus.SUCCEEDED,
    ("baseline", GOOD): ObservationStatus.REJECTED,
    ("baseline", STRANGER): ObservationStatus.REJECTED,
}


@pytest.mark.parametrize(("selection", "commit"), CASES)
@pytest.mark.parametrize("verify", [False, True])
def test_a_selection_is_adopted_only_when_it_is_the_runs_own(
    tmp_path: Path, selection: str, commit: str, *, verify: bool
) -> None:
    built = harness_in(tmp_path)
    tag = tag_named("selected")
    chosen = (
        TrustedBaseline(revision=revision(commit))
        if selection == "baseline"
        else RetainedCandidate(settlement_id=SettlementId(root="s"), revision=revision(commit))
    )
    step: Request = (
        VerifyAdoption(request_id=tag, scope=SCOPE, selection=chosen)
        if verify
        else AdoptRevision(request_id=tag, scope=SCOPE, selection=chosen)
    )

    async def main() -> None:
        await retain(built, GOOD)
        result = await built.runner().run(step, tag, 1, DEADLINE, CancelSignal())
        assert isinstance(result, Finished)
        assert result.observation.status is CASES[(selection, commit)]
        published = bool(built.world.published)
        assert published is (CASES[(selection, commit)] is ObservationStatus.SUCCEEDED)

    run_virtual(built.clock, main())


def test_a_publication_the_workspace_refuses_is_a_failed_adoption_not_a_raise(
    tmp_path: Path,
) -> None:
    built = harness_in(tmp_path)
    tag = tag_named("refused")
    built.world.refuses = True

    async def main() -> None:
        await retain(built, GOOD)
        result = await built.runner().run(adopt(tag, GOOD), tag, 1, DEADLINE, CancelSignal())
        assert isinstance(result, Finished)
        assert result.observation.status is ObservationStatus.FAILED
        assert result.observation.diagnostic == "publication refused"

    run_virtual(built.clock, main())


def test_a_resumed_run_keeps_the_operators_tree_and_publishes_nothing(tmp_path: Path) -> None:
    built = harness_in(tmp_path)
    tag = tag_named("protected")

    async def main() -> None:
        await retain(built, GOOD)
        runner = built.runner(publishes=False)
        matching = await runner.run(adopt(tag, BASE), tag, 1, DEADLINE, CancelSignal())
        differing = await runner.run(adopt(tag, GOOD), tag, 1, DEADLINE, CancelSignal())
        assert isinstance(matching, Finished)
        assert matching.observation.status is ObservationStatus.SUCCEEDED
        assert isinstance(differing, Finished)
        assert differing.observation.status is ObservationStatus.REJECTED
        assert built.world.published == []

    run_virtual(built.clock, main())


def test_a_cancelled_publication_returns_cancelled_and_clear_waits_for_the_workspace(
    tmp_path: Path,
) -> None:
    built = harness_in(tmp_path)
    tag = tag_named("cancelled")

    async def main() -> None:
        await retain(built, HANGS)
        cancel = CancelSignal()
        task = asyncio.ensure_future(
            built.runner().run(adopt(tag, HANGS), tag, 1, DEADLINE, cancel)
        )

        async def publishing() -> int:
            return built.world.publishing

        await yield_until(publishing, lambda count: count > 0, "the publication")
        cancel.set(CancelReason.PAUSE_NOW)
        assert await asyncio.wait_for(task, HANG_GUARD_S) == Cancelled(
            reason=CancelReason.PAUSE_NOW
        )
        built.world.unreachable = True
        with pytest.raises(ClearUnavailableError):
            await built.runner().clear(adopt(tag, GOOD), tag)

    run_virtual(built.clock, main())
