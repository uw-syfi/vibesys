"""Sequences of exclusive-root attempts, with the host dying at any durable boundary.

A serial strategy runs one attempt after another in the run's own root directory. Core ends
each attempt with a `DiscardWorkspace`; the next attempt is granted the root, and its tree
must be the plan's base again, with the run's declared artifact directory kept.

Every durable write goes through one `KeyedDocuments`, so a store that raises at its n-th
operation models a host that died between two durable writes. The restore of the root tree is
a sequence of Git steps (reset the index, clean untracked and ignored files, restore tracked
files), and a repository wrapper that raises at the n-th step models a host that died before,
between or after them. After the death a new executor over the same disk repeats the request,
as a restarted shell does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import HealthCheck, example, given, settings
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
    KeyedDocuments,
    ReceiptEvidenceLedger,
    RuntimeWorkspaceRequests,
    StoreWorkspaceReceipts,
    revision_ref,
)
from vs_sim.api.testing import InlineBlockingRunner, SimThreads, examples, swept

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
    from vs_project.api import GitRepository
    from vs_runtime.api.infrastructure import RuntimeWorkspace

# Each example builds real workspaces. A pull request draws a sample; the nightly depth
# (`--depth=nightly`) searches deeper.

_ROOT = WorkspaceMode.EXCLUSIVE_ROOT
_ARTIFACT_DIR = "profile"
_PLAN = f"{_ARTIFACT_DIR}/plan.md"
_NEVER = 10**6
_WRITES_PER_REQUEST = 9
_RESTORE_POINTS = 4
"""Before the index reset, before the clean, before the tracked restore, and after it."""
_IGNORE_RULES = "*.cache\nbuild/\n"
_LEFTOVERS = (
    "scratch.txt",
    "untracked/notes.txt",
    "result.cache",
    "build/out.bin",
    "deep/er/object.cache",
)
"""Files an attempt may leave: untracked ones and ones the ignore rules hide."""


class MortalStore(KeyedDocuments):
    """A real store that raises at the n-th durable operation while armed."""

    def __init__(self, env: WorkspaceEnv) -> None:
        super().__init__(env.receipts_namespace(), SimThreads())
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


class RestoreCrash:
    """Counts the steps of a tree restore and raises at the n-th while armed."""

    def __init__(self) -> None:
        self.dies_at = _NEVER
        self.steps = 0

    def step(self) -> None:
        self.steps += 1
        if self.steps > self.dies_at:
            raise HostDied

    def disarm(self) -> None:
        self.dies_at = _NEVER
        self.steps = 0


class CrashingGit:
    """A repository whose tree-restore steps are crash points; all else passes through."""

    def __init__(self, git: Any, crash: RestoreCrash) -> None:  # noqa: ANN401  # lint-waiver: LW-402321 [ANN401]; forwards every method of the wrapped repository protocol unchanged.
        self._git = git
        self._crash = crash

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401  # lint-waiver: LW-402322 [ANN401]; forwards every method of the wrapped repository protocol unchanged.
        return getattr(self._git, name)

    def reset_index(self) -> None:
        self._crash.step()
        self._git.reset_index()

    def clean_untracked(self, **kwargs: Any) -> bool:  # noqa: ANN401  # lint-waiver: LW-402323 [ANN401]; forwards the wrapped call's keywords unchanged.
        self._crash.step()
        return cast("bool", self._git.clean_untracked(**kwargs))

    def restore_worktree(self, *args: Any) -> None:  # noqa: ANN401  # lint-waiver: LW-402324 [ANN401]; forwards the wrapped call's arguments unchanged.
        self._crash.step()
        self._git.restore_worktree(*args)
        self._crash.step()


@dataclass
class Host:
    """One run's disk and a restartable executor over it."""

    env: WorkspaceEnv
    crash: RestoreCrash = field(default_factory=RestoreCrash)

    def executor(self, store: KeyedDocuments) -> RuntimeWorkspaceRequests:
        return RuntimeWorkspaceRequests(
            self.env.hosts[-1],
            store,
            SettledRunInvocations(),
            blocking=InlineBlockingRunner(),
            evidence=ReceiptEvidenceLedger(store),
        )

    async def run(
        self, request: Request, *, dies_at: int = _NEVER, restore_dies_at: int = _NEVER
    ) -> ExecutionResult:
        """Execute *request*; if the host dies at its *dies_at*-th durable write or its
        *restore_dies_at*-th restore step, restart and repeat."""
        store = MortalStore(self.env)
        store.dies_at = dies_at
        self.crash.disarm()
        self.crash.dies_at = restore_dies_at
        try:
            return await self._execute(self.executor(store), request)
        except HostDied:
            self.crash.disarm()
            return await self._execute(self.executor(MortalStore(self.env)), request)
        finally:
            self.crash.disarm()

    async def _execute(
        self, executor: RuntimeWorkspaceRequests, request: Request
    ) -> ExecutionResult:
        outcome = await executor.execute(request, _context(request))
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
    restore_dies_at: int = _NEVER
    leftovers: tuple[str, ...] = ()


_ROUND = st.builds(
    Round,
    base_pick=st.integers(0, 10),
    snapshot=st.booleans(),
    ensure_dies_at=st.sampled_from([_NEVER, *range(9)]),
    discard_dies_at=st.sampled_from([_NEVER, *range(8)]),
    restore_dies_at=st.sampled_from([_NEVER, *range(_RESTORE_POINTS)]),
    leftovers=st.lists(st.sampled_from(_LEFTOVERS), unique=True).map(tuple),
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


def _is_ignored(name: str) -> bool:
    return name.endswith(".cache") or name.startswith("build/")


def _write_files(root: Path, names: tuple[str, ...] | list[str]) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"left by {name}\n", encoding="utf-8")


async def _commit_ignore_rules(root: RuntimeWorkspace) -> None:
    (root.path / ".gitignore").write_text(_IGNORE_RULES, encoding="utf-8")
    await root.snapshot("ignore rules")


async def _attempts(env: WorkspaceEnv, rounds: list[Round], crash: RestoreCrash) -> None:
    """Run one attempt per round in the root, checking the hold and each grant's tree."""
    host = Host(env, crash)
    root = env.hosts[-1].root
    await _commit_ignore_rules(root)
    assert root.revision is not None
    # Revisions a later attempt may use as its base, with the tree each one holds.
    bases = [(revision_ref(root.revision), _without_artifacts(root.path))]
    ended: list[AttemptRef] = []
    for index, step in enumerate(rounds):
        attempt = _attempt(f"a{index}")
        base, base_tree = bases[step.base_pick % len(bases)]
        plan = WorkspacePlan(mode=_ROOT, base=base)
        ensure = _ensure(attempt, f"ensure-{index}", plan)
        granted = await host.run(
            ensure, dies_at=step.ensure_dies_at, restore_dies_at=step.restore_dies_at
        )
        assert _status(granted) is ObservationStatus.SUCCEEDED
        assert host.holder() == attempt
        # The grant's tree is exactly the plan's base plus the artifact directory earlier
        # attempts kept: no untracked or ignored file an earlier attempt left remains.
        assert _without_artifacts(root.path) == base_tree
        expected_plan = None if index == 0 else f"step {index - 1}\n"
        kept = {name for name in _visible_files(root.path) if name.startswith(f"{_ARTIFACT_DIR}/")}
        assert kept == (set() if index == 0 else {_PLAN})
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
        _write_files(root.path, step.leftovers)
        # A snapshot commits tracked and untracked files but not the ignored ones.
        worked_tree = {
            name: text
            for name, text in _without_artifacts(root.path).items()
            if not _is_ignored(name)
        }
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
    crash = RestoreCrash()
    with open_workspace_env(
        path,
        memory_paths=(_ARTIFACT_DIR,),
        wrap_git=lambda git: cast("GitRepository", CrashingGit(git, crash)),
    ) as env:
        asyncio.run(_attempts(env, rounds, crash))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


@settings(
    max_examples=examples(pr=2, nightly=25),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rounds=st.lists(_ROUND, min_size=1, max_size=4))
def test_the_root_is_held_until_its_attempt_is_discarded_and_every_grant_starts_from_base(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, rounds: list[Round]
) -> None:
    _play(tmp_path_factory, monkeypatch, rounds)


@pytest.mark.parametrize("dies_at", swept(_WRITES_PER_REQUEST, 3))
def test_a_host_that_dies_at_any_write_of_a_grant_or_a_release_loses_nothing(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, dies_at: int
) -> None:
    """The second attempt starts on a dirty root, so a lost restore would show."""
    round_ = Round(
        base_pick=0,
        snapshot=True,
        ensure_dies_at=dies_at,
        discard_dies_at=dies_at,
        leftovers=_LEFTOVERS,
    )
    _play(tmp_path_factory, monkeypatch, [round_, round_, round_])


@pytest.mark.parametrize("restore_dies_at", swept(_RESTORE_POINTS, 2))
def test_a_host_that_dies_before_during_or_after_the_restore_of_a_grant_loses_nothing(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    restore_dies_at: int,
) -> None:
    """A grant binds only after its restore finished, so a death anywhere in the restore
    repeats the restore instead of leaving a bound attempt on a dirty tree."""
    round_ = Round(
        base_pick=0,
        snapshot=False,
        ensure_dies_at=_NEVER,
        discard_dies_at=_NEVER,
        restore_dies_at=restore_dies_at,
        leftovers=_LEFTOVERS,
    )
    _play(tmp_path_factory, monkeypatch, [round_, round_, round_])


# -- replaying ensure and discard across ended generations ---------------------------------

_POOL = tuple(_attempt(name, generation) for name in ("p", "q") for generation in (0, 1))


@dataclass(frozen=True)
class Op:
    """One request in a replay order: a fresh ensure or discard, or a verbatim repeat."""

    kind: str
    pick: int


_OPS = st.builds(Op, kind=st.sampled_from(["ensure", "discard", "repeat"]), pick=st.integers(0, 50))


@dataclass
class _Model:
    """What the root's holder and the ended generations must be, by the exclusive-root rules."""

    holder: AttemptRef | None = None
    bound: set[AttemptRef] = field(default_factory=set)
    ended: set[AttemptRef] = field(default_factory=set)
    seen: dict[str, int] = field(default_factory=dict)
    """The highest generation of each attempt any request has named, granted or not."""

    def _admit(self, attempt: AttemptRef) -> bool:
        """Record the generation; ``False`` once a request named a higher one."""
        key = attempt.attempt_id.root
        if attempt.generation < self.seen.get(key, 0):
            return False
        self.seen[key] = attempt.generation
        return True

    def ensure(self, attempt: AttemptRef) -> ObservationStatus:
        if not self._admit(attempt):
            return ObservationStatus.REJECTED
        holder = self.holder
        if attempt in self.ended:
            return ObservationStatus.REJECTED
        if holder is not None and holder.attempt_id != attempt.attempt_id:
            return ObservationStatus.UNKNOWN
        if holder is not None and attempt.generation < holder.generation:
            return ObservationStatus.REJECTED
        self.holder = attempt
        self.bound.add(attempt)
        return ObservationStatus.SUCCEEDED

    def discard(self, attempt: AttemptRef) -> ObservationStatus:
        if not self._admit(attempt):
            return ObservationStatus.REJECTED
        if attempt in self.ended:
            return ObservationStatus.SUCCEEDED
        if attempt != self.holder:
            return ObservationStatus.REJECTED
        self.holder = None
        self.ended.add(attempt)
        return ObservationStatus.SUCCEEDED


async def _replay(env: WorkspaceEnv, ops: list[Op], crash: RestoreCrash) -> None:
    """Drive *ops* and check each result, the holder and the tree against the model."""
    host = Host(env, crash)
    root = env.hosts[-1].root
    await _commit_ignore_rules(root)
    base_tree = _visible_files(root.path)
    base = revision_ref(cast("str", root.revision))
    plan = WorkspacePlan(mode=_ROOT, base=base)
    model = _Model()
    sent: list[tuple[Request, AttemptRef, ObservationStatus]] = []
    for index, op in enumerate(ops):
        attempt = _POOL[op.pick % len(_POOL)]
        dirty_names = (*_LEFTOVERS, "candidate.py")
        # Files present before the first grant belong to the user; only attempts leave a mess.
        if StoreWorkspaceReceipts(MortalStore(env)).root_baseline() is not None:
            _write_files(root.path, dirty_names)
        before = _visible_files(root.path)
        replayed: Request | None = None
        slot = op.pick % len(sent) if sent else 0
        if op.kind == "repeat" and sent:
            replayed, attempt, answered = sent[slot]
            if answered is not ObservationStatus.UNKNOWN:
                # A sealed request answers again without effect: nothing changes.
                await host.run(replayed)
                assert host.holder() == model.holder
                assert _visible_files(root.path) == before
                continue
            # An Unknown answer is never stored, so repeating it is a new evaluation.
        was_new_hold = model.holder != attempt
        if replayed is not None:
            request: Request = replayed
            expected = model.ensure(attempt)
            released = False
        elif op.kind == "discard" and attempt in model.bound:
            request = DiscardWorkspace(**_common(attempt, f"op-{index}"), attempt=attempt)
            expected = model.discard(attempt)
            released = True
        elif op.kind == "ensure":
            request = _ensure(attempt, f"op-{index}", plan)
            expected = model.ensure(attempt)
            released = False
        else:
            continue
        if replayed is None:
            sent.append((request, attempt, expected))
        else:
            sent[slot] = (request, attempt, expected)  # the new answer is what a repeat sees
        result = await host.run(request)
        assert _status(result) is expected, (op, attempt, expected)
        assert host.holder() == model.holder
        granted = not released and expected is ObservationStatus.SUCCEEDED and was_new_hold
        # A new grant restores the base; every other request leaves the tree as it was.
        assert _visible_files(root.path) == (base_tree if granted else before)


@settings(
    max_examples=examples(pr=3, nightly=40),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(ops=st.lists(_OPS, min_size=1, max_size=14))
@example(  # a refused ensure of generation 1 still retires generation 0 of that attempt
    ops=[Op("ensure", 0), Op("ensure", 3), Op("ensure", 2)]
)
@example(  # a refused ensure is retried, not sealed, so repeating it after the release grants
    ops=[Op("ensure", 0), Op("ensure", 2), Op("discard", 0), Op("repeat", 1)]
)
@example(  # once that retry is granted it is sealed, and a later repeat has no effect
    ops=[
        Op("ensure", 2),
        Op("ensure", 0),
        Op("discard", 2),
        Op("repeat", 1),
        Op("ensure", 1),
        Op("repeat", 1),
    ]
)
def test_an_ended_generation_is_never_granted_again_and_a_stale_release_never_reports_success(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, ops: list[Op]
) -> None:
    path = tmp_path_factory.mktemp("replay")
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(path / "operator-state"))
    crash = RestoreCrash()
    with open_workspace_env(
        path,
        memory_paths=(_ARTIFACT_DIR,),
        wrap_git=lambda git: cast("GitRepository", CrashingGit(git, crash)),
    ) as env:
        asyncio.run(_replay(env, ops, crash))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


def test_a_replayed_ensure_of_an_ended_generation_is_refused_after_a_later_holder_ended(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ends, B takes the root and ends it, then A's ensure is replayed under a new id."""
    first, second = _attempt("p"), _attempt("q")
    ops = [
        Op("ensure", 0),  # p gen 0
        Op("discard", 0),
        Op("ensure", 2),  # q gen 0
        Op("discard", 2),
        Op("ensure", 0),  # p gen 0 again
    ]
    assert _POOL[0] == first
    assert _POOL[2] == second
    path = tmp_path_factory.mktemp("replay")
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(path / "operator-state"))
    crash = RestoreCrash()
    with open_workspace_env(
        path,
        memory_paths=(_ARTIFACT_DIR,),
        wrap_git=lambda git: cast("GitRepository", CrashingGit(git, crash)),
    ) as env:
        asyncio.run(_replay(env, ops, crash))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())
