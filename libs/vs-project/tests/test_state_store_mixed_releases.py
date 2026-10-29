"""At most one host holds a run's lease, whichever release each host runs and wherever one dies.

Older releases keep the lease inside ``store.json`` (version 1) and read nothing else; this
release keeps it in ``lease.json`` and moves it there on its first write to an older store. A
host of either release may run against the same run, as live runs are resumed from main and
from fix branches, and a host may die between any two durable steps of a write. Whatever
happens, every lease granted takes a larger epoch than any granted before it, at most one
granted lease is valid at any moment, and a host of this release can always go on.

The older release is modelled by ``_OlderRelease``: its lease rules, applied to the bytes of
``store.json`` as it reads and writes them (origin/main c5d46f25f).
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_project.api import (
    Committed,
    LocalAtomicWriteEffects,
    Project,
    StoredEnvelope,
    StoreFence,
    StoreFile,
    Unknown,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_project.api import AtomicWriteStream, StateStore

type Release = Literal["older", "this"]

_RUN = "run-1"
# What an older release wrote after acquiring at 100 for 40 s and committing at 112.5.
_V1_STORE = {
    "version": 1,
    "record": {"kind": "envelope", "revision": 1, "schema_version": 3, "payload": "eA=="},
    "fence": {"host_id": "host-a", "epoch": 1, "expires_at": 140.0},
    "observed_at": 112.5,
}
_V1_FENCE = StoreFence(host_id="host-a", epoch=1, expires_at=140.0)


class _HostDiedError(BaseException):
    """The host process died: nothing after this point of the write runs."""


type Failure = Literal["dies", "fails"]


class _FailingEffects(LocalAtomicWriteEffects):
    """Local durability effects that fail at their ``at``-th step.

    A host that ``fails`` sees an ``OSError`` at the step, and the steps are every file sync,
    rename, and the first directory sync after a rename (a write syncs each directory up to the
    project root, and a failure at any of them reaches the writer at the same point). A host that ``dies`` runs nothing after the step, and its steps
    are the renames only: each write syncs the directory before the next write begins, so the
    files a death leaves are those of the renames before it, and a death at a sync leaves the
    files of a death at the next rename (the last rename lost) or of no death (it was kept).
    The syncs themselves are skipped: the test decides what a crash keeps, so making the
    bytes durable would only slow every example down.
    """

    def __init__(self, at: int | None, failure: Failure = "dies") -> None:
        self.steps = 0
        self._renamed = False
        self._at = at
        self._failure = failure

    def _step(self, *, rename: bool = False) -> None:
        if self._failure == "dies" and not rename:
            return
        self.steps += 1
        if self.steps != self._at:
            return
        if self._failure == "dies":
            raise _HostDiedError
        message = f"injected failure at durable step {self.steps}"
        raise OSError(message)

    def sync_file(self, stream: AtomicWriteStream) -> None:
        del stream  # nothing is made durable (see the class)
        self._step()

    def sync_existing_file(self, path: Path) -> None:
        del path  # nothing is made durable (see the class)
        self._step()

    def replace(self, temporary: Path, destination: Path) -> None:
        self._step(rename=True)
        super().replace(temporary, destination)
        self._renamed = True

    def sync_directory(self, directory: Path) -> None:
        del directory  # nothing is made durable (see the class)
        if self._renamed:
            self._renamed = False
            self._step()


class _RefusedError(Exception):
    """The older release refuses a ``store.json`` it cannot read."""


class _OlderRelease:
    """The lease rules of a release that keeps the lease in ``store.json`` version 1."""

    def __init__(self, root: Path) -> None:
        self._path = Project.open(root).state.state_store_namespace(_RUN).external_directory()

    def _read(self) -> dict[str, object]:
        path = self._path / StoreFile.RECORD
        if not path.exists():
            return {"version": 1, "record": None, "fence": None, "observed_at": 0}
        document = json.loads(path.read_bytes())
        if document.get("version", 1) != 1:
            raise _RefusedError
        return document

    def _write(self, document: dict[str, object]) -> None:
        self._path.mkdir(parents=True, exist_ok=True)
        (self._path / StoreFile.RECORD).write_bytes(json.dumps(document).encode())

    @staticmethod
    def _fence(document: dict[str, object]) -> StoreFence | None:
        fence = document["fence"]
        return None if fence is None else StoreFence.model_validate(fence)

    def _valid(self, document: dict[str, object], fence: StoreFence, now: float) -> bool:
        current = self._fence(document)
        observed_at = document["observed_at"]
        assert isinstance(observed_at, int | float)
        return (
            current is not None
            and (current.host_id, current.epoch) == (fence.host_id, fence.epoch)
            and observed_at <= now < current.expires_at
        )

    def acquire(self, host_id: str, now: float, duration: float) -> StoreFence | None:
        document = self._read()
        current = self._fence(document)
        observed_at = document["observed_at"]
        assert isinstance(observed_at, int | float)
        if now < observed_at or (current is not None and now < current.expires_at):
            return None
        epoch = 1 if current is None else current.epoch + 1
        fence = StoreFence(host_id=host_id, epoch=epoch, expires_at=now + duration)
        self._write({**document, "fence": fence.model_dump(), "observed_at": now})
        return fence

    def renew(self, fence: StoreFence, now: float, duration: float) -> StoreFence | None:
        document = self._read()
        if not self._valid(document, fence, now):
            return None
        renewed = fence.model_copy(update={"expires_at": max(fence.expires_at, now + duration)})
        self._write({**document, "fence": renewed.model_dump(), "observed_at": now})
        return renewed

    def release(self, fence: StoreFence, now: float) -> bool:
        document = self._read()
        if not self._valid(document, fence, now):
            return False
        released = fence.model_copy(update={"expires_at": now})
        self._write({**document, "fence": released.model_dump(), "observed_at": now})
        return True

    def verify(self, fence: StoreFence, now: float) -> bool:
        return self._valid(self._read(), fence, now)

    def commit(self, fence: StoreFence, now: float) -> bool:
        document = self._read()
        if not self._valid(document, fence, now):
            return False
        record = document["record"]
        revision = 0 if not isinstance(record, dict) else record["revision"] + 1
        envelope = StoredEnvelope(revision=revision, schema_version=3, payload=b"older")
        self._write(
            {**document, "record": json.loads(envelope.model_dump_json()), "observed_at": now}
        )
        return True


@dataclass
class _Holder:
    release: Release
    fence: StoreFence


class _World:
    """One run's store, the hosts that ever held its lease, and the invariants over them."""

    def __init__(self, root: Path, holders: list[_Holder]) -> None:
        self._project = Project.open(root)
        self._older = _OlderRelease(root)
        self.holders = holders
        self.granted = [holder.fence.epoch for holder in holders]

    def store(self, effects: _FailingEffects | None = None) -> StateStore:
        """A host of this release, newly started: it trusts nothing it did not read."""
        return self._project.state_store(_RUN, effects=effects or _FailingEffects(None))

    def older(self) -> _OlderRelease:
        return self._older

    def valid(self, holder: _Holder, now: float) -> bool:
        if holder.release == "this":
            return self.store().verify(holder.fence, now)
        try:
            return self.older().verify(holder.fence, now)
        except _RefusedError:
            return False

    def grant(self, release: Release, fence: StoreFence | None) -> None:
        if fence is None:
            return
        assert fence.epoch > max(self.granted, default=0), (fence, self.granted)
        self.granted.append(fence.epoch)
        self.holders.append(_Holder(release, fence))

    def check(self, now: float) -> None:
        valid = [holder for holder in self.holders if self.valid(holder, now)]
        assert len(valid) <= 1, valid

    def act(self, action: _Action, now: float) -> None:
        """One host's step; a host of the older release that cannot read the store does nothing.

        A host of this release dies or fails at the action's drawn durable step, if any: a
        write that reports a failure grants nothing, and a host that died reports nothing.
        """
        kind, release, index, duration, fault = action
        effects = None if fault is None else _FailingEffects(fault[1], fault[0])
        try:
            if kind == "acquire":
                host = f"host-{len(self.granted)}"
                if release == "this":
                    self.grant(release, self.store(effects).acquire(host, now, duration))
                else:
                    self.grant(release, self.older().acquire(host, now, duration))
                return
            if not self.holders:
                return
            holder = self.holders[index % len(self.holders)]
            self._hold(holder, kind, now, duration, effects)
        except (_RefusedError, _HostDiedError):
            return
        except OSError:
            # Only an injected failure raises, and only in a host of this release.
            assert effects is not None

    def _hold(
        self,
        holder: _Holder,
        kind: str,
        now: float,
        duration: float,
        effects: _FailingEffects | None,
    ) -> None:
        older = None if holder.release == "this" else self.older()
        if kind == "renew":
            renewed = (
                self.store(effects).renew(holder.fence, now, duration)
                if older is None
                else older.renew(holder.fence, now, duration)
            )
            holder.fence = renewed or holder.fence
        elif kind == "release":
            _ = (
                self.store(effects).release(holder.fence, now)
                if older is None
                else older.release(holder.fence, now)
            )
        elif older is None:
            store = self.store(effects)
            current = store.load()
            revision = 0 if current is None else current.revision + 1
            envelope = StoredEnvelope(revision=revision, schema_version=3, payload=b"this")
            outcome = store.commit(
                None if current is None else current.revision, envelope, holder.fence, now
            )
            # Without a fault, only a host that lost the lease fails to commit; a failing
            # host may also learn nothing about its commit.
            assert (
                isinstance(outcome, Committed)
                or (effects is not None and isinstance(outcome, Unknown))
                or not store.verify(holder.fence, now)
            )
        else:
            older.commit(holder.fence, now)


# Where a host of this release dies or fails during one action: its kind and durable step.
type _Fault = tuple[Failure, int] | None
type _Action = tuple[Literal["acquire", "renew", "release", "commit"], Release, int, float, _Fault]

# The first write of this release, by a host that may die at any of its durable steps.
_FIRST_WRITES: dict[str, Callable[[StateStore], object]] = {
    "acquire": lambda store: store.acquire("host-b", now=150, duration=100),
    "renew": lambda store: store.renew(_V1_FENCE, now=120, duration=100),
    "release": lambda store: store.release(_V1_FENCE, now=120),
    "commit": lambda store: store.commit(
        1, StoredEnvelope(revision=2, schema_version=3, payload=b"next"), _V1_FENCE, now=120
    ),
}
_STARTS = ("fresh", *sorted(_FIRST_WRITES))

# A write of this release takes at most three renames (copy the lease, rewrite store.json,
# write the lease), and fails at one of at most nine steps (each one's file sync, rename and
# directory sync).
_FAULTS = (
    st.none()
    | st.tuples(st.just("dies"), st.integers(1, 3))
    | st.tuples(st.just("fails"), st.integers(1, 9))
)
_ACTIONS = st.lists(
    st.tuples(
        st.sampled_from(("acquire", "renew", "release", "commit")),
        st.sampled_from(("older", "this")),
        st.integers(0, 3),
        st.integers(1, 200),
        _FAULTS,
    ),
    max_size=6,
)
_GAPS = st.lists(st.integers(0, 120), min_size=6, max_size=6)


def _start(root: Path, start: str, at: int | None, failure: Failure = "dies") -> tuple[_World, int]:
    """A run whose first write by this release fails at step ``at``; the steps the write took."""
    directory = Project.open(root).state.state_store_namespace(_RUN).external_directory()
    effects = _FailingEffects(at, failure)
    if start == "fresh":
        world = _World(root, [])
        with suppress(_HostDiedError, OSError):
            world.grant("this", world.store(effects).acquire("host-b", now=150, duration=100))
        return world, effects.steps
    directory.mkdir(parents=True, exist_ok=True)
    (directory / StoreFile.RECORD).write_bytes(json.dumps(_V1_STORE).encode())
    # host-a runs the older release, unless the first write is its own, on this release.
    release: Release = "older" if start == "acquire" else "this"
    world = _World(root, [_Holder(release, _V1_FENCE)])
    try:
        result = _FIRST_WRITES[start](world.store(effects))
    except _HostDiedError:
        return world, effects.steps
    except OSError:
        # A write that reports a failure published nothing a caller relies on: a commit that
        # raises left the record as it was (a published one is Committed or Unknown).
        loaded = world.store().load()
        assert loaded is not None
        assert loaded.revision == 1
        return world, effects.steps
    if start == "acquire":
        assert isinstance(result, StoreFence | None)
        world.grant("this", result)
    elif start == "renew" and isinstance(result, StoreFence):
        world.holders[0].fence = result
    return world, effects.steps


def _steps(root: Path, start: str, failure: Failure) -> int:
    """How many durable steps the first write takes when its host does not die."""
    return _start(root, start, None, failure)[1]


@pytest.mark.parametrize("failure", ["dies", "fails"])
@pytest.mark.parametrize("start", _STARTS)
@settings(max_examples=examples(pr=10, nightly=40), deadline=None, database=None)
@given(actions=_ACTIONS, gaps=_GAPS)
# The review's reproduction: this release takes over after the older host's lease expired and
# dies before store.json is rewritten; an older host then takes over from store.json, and a
# host of this release later takes over from lease.json while the older host still holds it.
@example(
    actions=[
        ("acquire", "older", 0, 160, None),
        ("commit", "older", 2, 1, None),
        ("acquire", "this", 0, 30, None),
    ],
    gaps=[0, 1, 40, 0, 0, 0],
)
# A second migration that dies after rewriting store.json: the first one died after copying
# the lease, an older host then took a larger epoch from store.json, and the second attempt
# must copy that lease again, or the next host of this release reuses the older host's epoch.
@example(
    actions=[
        ("acquire", "older", 0, 30, None),
        ("acquire", "this", 0, 30, ("dies", 2)),
        ("acquire", "this", 0, 30, None),
    ],
    gaps=[0, 40, 0, 0, 0, 0],
)
def test_no_two_hosts_hold_the_lease_wherever_the_migrating_host_dies(
    tmp_path_factory: pytest.TempPathFactory,
    start: str,
    failure: Failure,
    actions: list[_Action],
    gaps: list[int],
) -> None:
    total = _steps(tmp_path_factory.mktemp("count"), start, failure)
    for at in range(1, total + 2):
        world, _ = _start(tmp_path_factory.mktemp("run"), start, at, failure)
        now = 151.0
        world.check(now)
        for action, gap in zip(actions, gaps, strict=False):
            now += gap
            world.act(action, now)
            world.check(now)
        # A host of this release can always go on: no crash leaves a store it calls damaged,
        # and every lease has expired by now, so it is granted a larger epoch than any before.
        last = world.store().acquire("host-last", now=now + 1000, duration=1)
        assert last is not None
        world.grant("this", last)
