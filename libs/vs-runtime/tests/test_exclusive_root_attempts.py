"""Sequences of exclusive-root attempts, with the host dying at any durable boundary.

A serial strategy runs one attempt after another in the run's own root directory. Core ends
each attempt with a `DiscardWorkspace`; the next attempt is granted the root, and its tree
must be the plan's base again, with the run's declared artifact directory kept.

Every durable write goes through one `ReceiptStore`, so a store that raises at its n-th
operation models a host that died between two durable writes. After the death a new executor
over the same disk repeats the request, as a restarted shell does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.session_world import SettledRunInvocations
from tests.support.workspace_world import WorkspaceEnv, open_workspace_env

from vs_core.api import (
    DiscardWorkspace,
    EnsureWorkspace,
    ObservationStatus,
    RetainRevision,
    SnapshotAndRetain,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_runtime.api.core import (
    ExecutionResult,
    ReceiptStore,
    RuntimeWorkspaceRequests,
    StoreWorkspaceReceipts,
    revision_ref,
)

from .test_session_access import HostDied
from .test_workspace_requests import (
    _attempt,
    _common,
    _context,
    _status,
    _visible_files,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_core.api import AttemptRef, Request, RevisionRef

_ROOT = WorkspaceMode.EXCLUSIVE_ROOT
_ARTIFACT_DIR = "roadmap"
_PLAN = f"{_ARTIFACT_DIR}/plan.md"
_NEVER = 10**6
_WRITES_PER_REQUEST = 9
_BASE_TREE_KEY = "candidate.py"


class MortalStore(ReceiptStore):
    """A real store that raises at the n-th durable operation while armed."""

    def __init__(self, env: WorkspaceEnv) -> None:
        super().__init__(env.receipts_namespace())
        self.dies_at = _NEVER
        self.operations = 0

    def _tick(self) -> None:
        self.operations += 1
        if self.operations > self.dies_at:
            raise HostDied

    def modify(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401  # lint-waiver: LW-402320 [ANN401]; forwards the generic base signature unchanged.
        self._tick()
        return super().modify(*args, **kwargs)

    def replace(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        self._tick()
        super().replace(family, part, key, receipt)


@dataclass
class Host:
    """One run's disk and a restartable executor over it."""

    env: WorkspaceEnv
    epoch: int = 1

    def executor(self, store: ReceiptStore) -> RuntimeWorkspaceRequests:
        return RuntimeWorkspaceRequests(self.env.hosts[-1], store, SettledRunInvocations())

    async def run(self, request: Request, *, dies_at: int = _NEVER) -> ExecutionResult:
        """Execute *request*; if the host dies at its *dies_at*-th write, restart and repeat."""
        store = MortalStore(self.env)
        store.dies_at = dies_at
        try:
            return await self._execute(self.executor(store), request)
        except HostDied:
            self.epoch += 1
            return await self._execute(self.executor(MortalStore(self.env)), request)

    async def _execute(
        self, executor: RuntimeWorkspaceRequests, request: Request
    ) -> ExecutionResult:
        outcome = await executor.execute(request, _context(request, epoch=self.epoch))
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome

    def holder(self) -> AttemptRef | None:
        """The attempt that holds the root, as another host would read it."""
        receipts = StoreWorkspaceReceipts(MortalStore(self.env))
        held = receipts.root_holder()
        return None if held is None or receipts.is_released(held) else held


def _ensure(attempt: AttemptRef, name: str, plan: WorkspacePlan) -> EnsureWorkspace:
    return EnsureWorkspace(**_common(attempt, name), attempt=attempt, plan=plan)


@dataclass(frozen=True)
class Round:
    """What one attempt does and where the host dies while it is granted and ended."""

    base_pick: int
    snapshot: bool
    ensure_dies_at: int
    discard_dies_at: int


_ROUND = st.builds(
    Round,
    base_pick=st.integers(0, 10),
    snapshot=st.booleans(),
    ensure_dies_at=st.sampled_from([_NEVER, *range(9)]),
    discard_dies_at=st.sampled_from([_NEVER, *range(8)]),
)


async def _retain(host: Host, attempt: AttemptRef, name: str, revision: RevisionRef) -> None:
    kept = await host.run(
        RetainRevision(
            **_common(attempt, name), attempt=attempt, revision=revision, retention="candidate"
        )
    )
    assert _status(kept) is ObservationStatus.SUCCEEDED


def _without_artifacts(root: Path) -> dict[str, str]:
    return {
        name: text
        for name, text in _visible_files(root).items()
        if not name.startswith(f"{_ARTIFACT_DIR}/")
    }


async def _attempts(env: WorkspaceEnv, rounds: list[Round]) -> None:
    """Run one attempt per round in the root, checking the hold and each grant's tree."""
    host = Host(env)
    root = env.hosts[-1].root
    assert root.revision is not None
    # Revisions a later attempt may use as its base, with the tree each one holds.
    bases = [(revision_ref(root.revision), _without_artifacts(root.path))]
    ended: list[AttemptRef] = []
    for index, step in enumerate(rounds):
        attempt = _attempt(f"a{index}")
        base, base_tree = bases[step.base_pick % len(bases)]
        plan = WorkspacePlan(mode=_ROOT, base=base)
        ensure = _ensure(attempt, f"ensure-{index}", plan)
        granted = await host.run(ensure, dies_at=step.ensure_dies_at)
        assert _status(granted) is ObservationStatus.SUCCEEDED
        assert host.holder() == attempt
        # The grant's tree is the plan's base plus the artifacts earlier attempts left.
        assert _without_artifacts(root.path) == base_tree
        expected_plan = None if index == 0 else f"step {index - 1}\n"
        found = _visible_files(root.path).get(_PLAN)
        assert found == expected_plan
        # While it holds the root, no other attempt gets it and no ended attempt's
        # repeated discard (a stale one) releases it.
        rival = _attempt(f"rival-{index}")
        rival = await host.run(_ensure(rival, f"rival-{index}", plan))
        assert _status(rival) is ObservationStatus.UNKNOWN
        for old in ended:
            name = f"stale-{old.attempt_id.root}-{index}"
            stale = await host.run(DiscardWorkspace(**_common(old, name), attempt=old))
            assert _status(stale) is ObservationStatus.SUCCEEDED
            assert host.holder() == attempt
        # The attempt works: a tracked edit, a new file and its run-scoped artifact.
        (root.path / "candidate.py").write_text(f"VALUE = {index + 10}\n", encoding="utf-8")
        (root.path / "scratch.txt").write_text("x\n", encoding="utf-8")
        (root.path / _PLAN).parent.mkdir(exist_ok=True)
        (root.path / _PLAN).write_text(f"step {index}\n", encoding="utf-8")
        worked_tree = _without_artifacts(root.path)
        if index == 0:
            # The first base becomes a retained revision of the run, so a later
            # attempt may name it after the root's head has moved on.
            await _retain(host, attempt, "initial", bases[0][0])
        if step.snapshot:
            kept = await host.run(
                SnapshotAndRetain(
                    **_common(attempt, f"snap-{index}"), attempt=attempt, retention="wip"
                )
            )
            assert _status(kept) is ObservationStatus.SUCCEEDED
            assert kept.observation.revision is not None
            await _retain(host, attempt, f"candidate-{index}", kept.observation.revision)
            bases.append((kept.observation.revision, worked_tree))
            assert host.holder() == attempt
        discard = DiscardWorkspace(**_common(attempt, f"discard-{index}"), attempt=attempt)
        result = await host.run(discard, dies_at=step.discard_dies_at)
        assert _status(result) is ObservationStatus.SUCCEEDED
        assert host.holder() is None
        ended.append(attempt)


def _play(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, rounds: list[Round]
) -> None:
    path = tmp_path_factory.mktemp("rounds")
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(path / "operator-state"))
    with open_workspace_env(path, memory_paths=(_ARTIFACT_DIR,)) as env:
        asyncio.run(_attempts(env, rounds))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(rounds=st.lists(_ROUND, min_size=1, max_size=4))
def test_the_root_is_held_until_its_attempt_is_discarded_and_every_grant_starts_from_base(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, rounds: list[Round]
) -> None:
    _play(tmp_path_factory, monkeypatch, rounds)


@pytest.mark.parametrize("dies_at", range(_WRITES_PER_REQUEST))
def test_a_host_that_dies_at_any_write_of_a_grant_or_a_release_loses_nothing(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, dies_at: int
) -> None:
    """The second attempt starts on a dirty root, so a lost restore would show."""
    round_ = Round(base_pick=0, snapshot=True, ensure_dies_at=dies_at, discard_dies_at=dies_at)
    _play(tmp_path_factory, monkeypatch, [round_, round_, round_])
