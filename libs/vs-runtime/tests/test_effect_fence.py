"""The effect fence: whichever of entering and closing comes first decides, across restarts."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import HostFence, HostId, RequestId
from vs_project.api import Project
from vs_runtime.api.core import (
    Applied,
    EffectFences,
    Entered,
    Fenced,
    FenceKey,
    FenceKind,
    Indeterminate,
    MayHold,
    NotApplied,
    NothingHeld,
    ObservationFacts,
    OwnerNotApplied,
    ReceiptStore,
    Removed,
    StillCreatingError,
    Unrecorded,
)
from vs_sim.api.testing import SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_KEYS = (FenceKey(FenceKind.INVOCATION, "s/i1"), FenceKey(FenceKind.SESSION_EPISODE, "s/e1"))
_REQUESTS = ("dispatch-a", "dispatch-b", "cancel-r")

_operation = st.tuples(
    st.sampled_from(("enter", "leave", "close")),
    st.sampled_from(range(len(_KEYS))),
    st.sampled_from(_REQUESTS),
    st.integers(min_value=1, max_value=3),
)


@dataclass
class _Model:
    """What the fence must answer: who entered, whether they left, whether it closed."""

    entries: dict[str, tuple[int, bool]] = field(default_factory=dict)
    closed: bool = False


def _host(epoch: int) -> HostFence:
    return HostFence(host_id=HostId(root="host"), epoch=epoch)


@settings(max_examples=examples(pr=10, nightly=400), deadline=None)
@given(operations=st.lists(_operation, max_size=12))
def test_enter_and_close_answer_as_the_model_does_and_nothing_held_stays_true(
    operations: list[tuple[str, int, str, int]],
) -> None:
    """Every operation on a restarted host; a NothingHeld answer is never followed by an entry."""
    models = [_Model() for _ in _KEYS]
    proven = [False for _ in _KEYS]
    with tempfile.TemporaryDirectory() as raw:
        namespace = Project.open(Path(raw)).state.state_store_namespace("run")
        for operation, index, request, epoch in operations:
            # A new store each time: the answer comes from disk, as on a restarted host.
            fences, key, model = (
                EffectFences(ReceiptStore(namespace, threads=SimThreads())),
                _KEYS[index],
                models[index],
            )
            by = RequestId(root=request)
            match operation:
                case "enter":
                    admission = fences.enter(key, by=by, host=_host(epoch))
                    if model.closed:
                        assert admission == Fenced(key, entered_before=request in model.entries)
                    else:
                        assert admission == Entered(key)
                        model.entries[request] = (epoch, False)
                    assert not (proven[index] and isinstance(admission, Entered))
                case "leave":
                    fences.leave(key, by=by)
                    if request in model.entries:
                        model.entries[request] = (model.entries[request][0], True)
                case _:
                    closure, evidence = fences.close_and_load(
                        key, by=by, host=_host(epoch), load=lambda: None
                    )
                    model.closed = True
                    if not model.entries:
                        assert isinstance(closure, NothingHeld)
                        proven[index] = True
                        continue
                    assert isinstance(closure, MayHold)
                    live = any(not left and at >= epoch for at, left in model.entries.values())
                    assert closure.live == live
                    if live:
                        with pytest.raises(StillCreatingError):
                            fences.cleared(closure, evidence)
                    else:
                        assert fences.cleared(closure, evidence).key == key
                        proven[index] = True


def test_only_the_fence_proves_that_nothing_is_held() -> None:
    """``ObservationFacts.never_held`` takes a NothingHeld, and only EffectFences makes one."""
    key = _KEYS[0]
    with pytest.raises(TypeError):
        NothingHeld(key, _mint=object())
    with pytest.raises(TypeError):
        MayHold(key, live=False, _mint=object())


def test_a_proof_copied_or_meant_for_another_fence_proves_nothing() -> None:
    """A proof cannot be copied, and it holds only for the fence it closed.

    Before, ``dataclasses.replace`` kept the private mint, so a copy with another key, or a
    live closure copied as finished, passed as the fence's own answer (and facts that hold
    nothing could be copied the same way); and ``never_held`` accepted a proof without
    checking whose fence it closed.
    """
    with tempfile.TemporaryDirectory() as directory:
        fences = EffectFences(
            ReceiptStore(
                Project.open(Path(directory)).state.state_store_namespace("run"),
                threads=SimThreads(),
            )
        )
        closed, other = _KEYS
        proof = fences.close(closed, by=RequestId(root="cancel-r"), host=_host(1))
        assert isinstance(proof, NothingHeld)
        fences.enter(other, by=RequestId(root="dispatch-a"), host=_host(1))
        live = fences.close(other, by=RequestId(root="cancel-r"), host=_host(1))
        assert isinstance(live, MayHold)
        assert live.live

        facts = ObservationFacts.never_held(proof, closed, "closed")
        assert facts.holds_nothing
        with pytest.raises(TypeError, match="another fence"):
            ObservationFacts.never_held(proof, other, "not this fence")
        with pytest.raises((TypeError, ValueError), match="_mint"):
            replace(proof, key=other)
        with pytest.raises((TypeError, ValueError), match="_mint"):
            replace(live, live=False)
        with pytest.raises(TypeError, match="only from never_held"):
            replace(facts, diagnostic="copied")


def test_evidence_clears_only_the_fence_it_is_about() -> None:
    """``cleared`` requires evidence that the entries hold nothing, about the closed fence.

    Before, ``cleared(closure)`` took the caller's word: any finished closure became a proof.
    """
    with tempfile.TemporaryDirectory() as directory:
        fences = EffectFences(
            ReceiptStore(
                Project.open(Path(directory)).state.state_store_namespace("run"),
                threads=SimThreads(),
            )
        )
        closed, other = _KEYS
        fences.enter(closed, by=RequestId(root="dispatch-a"), host=_host(1))
        fences.leave(closed, by=RequestId(root="dispatch-a"))
        closure, unrecorded = fences.close_and_load(
            closed, by=RequestId(root="cancel-r"), host=_host(1), load=lambda: None
        )
        _, elsewhere = fences.close_and_load(
            other, by=RequestId(root="cancel-r"), host=_host(1), load=lambda: None
        )
        assert isinstance(closure, MayHold)
        assert not closure.live
        assert isinstance(elsewhere, Unrecorded)
        with pytest.raises(TypeError, match="clears no other fence"):
            fences.cleared(closure, elsewhere)
        assert isinstance(unrecorded, Unrecorded)
        assert fences.cleared(closure, unrecorded).key == closed


def test_a_found_record_is_returned_instead_of_evidence() -> None:
    """``close_and_load`` mints Unrecorded only when the load under the closing lock found nothing."""
    with tempfile.TemporaryDirectory() as directory:
        fences = EffectFences(
            ReceiptStore(
                Project.open(Path(directory)).state.state_store_namespace("run"),
                threads=SimThreads(),
            )
        )
        key = _KEYS[0]
        _, found = fences.close_and_load(
            key, by=RequestId(root="cancel-r"), host=_host(1), load=lambda: "binding"
        )
        assert found == "binding"


@pytest.mark.parametrize("evidence", [Unrecorded, Removed, OwnerNotApplied])
def test_evidence_comes_only_from_the_step_that_establishes_it(evidence: type) -> None:
    """Before, any caller could construct evidence and clear a fence on its own word."""
    with pytest.raises(TypeError, match="comes only from the step"):
        evidence(_KEYS[0], object())


def _closed_after_an_entry(fences: EffectFences) -> MayHold:
    key = _KEYS[0]
    fences.enter(key, by=RequestId(root="dispatch-a"), host=_host(1))
    fences.leave(key, by=RequestId(root="dispatch-a"))
    closure = fences.close(key, by=RequestId(root="cancel-r"), host=_host(1))
    assert isinstance(closure, MayHold)
    return closure


@pytest.mark.parametrize(
    "answer", [NotApplied(), Applied(outcome={}), Indeterminate("unreadable")], ids=type
)
async def test_only_a_not_applied_the_fence_asked_for_after_the_close_is_evidence(
    answer: NotApplied | Applied | Indeterminate,
) -> None:
    """The fence asks the owner itself, after the close; only its NotApplied answer clears.

    Before, ``not_applied(closure, answer)`` took any ``NotApplied`` the caller held, one built
    on the spot or answered before the close, and minted evidence from it.
    """
    with tempfile.TemporaryDirectory() as directory:
        fences = EffectFences(
            ReceiptStore(
                Project.open(Path(directory)).state.state_store_namespace("run"),
                threads=SimThreads(),
            )
        )
        closure = _closed_after_an_entry(fences)
        asked_closed: list[bool] = []

        async def inspect() -> NotApplied | Applied | Indeterminate:
            asked_closed.append(fences.is_closed(closure.key))
            return answer

        found = await fences.ask_owner(closure, inspect)
        assert asked_closed == [True]
        if isinstance(answer, NotApplied):
            assert isinstance(found, OwnerNotApplied)
            assert fences.cleared(closure, found).key == closure.key
        else:
            assert found is answer


@pytest.mark.parametrize("left_behind", [False, True])
async def test_removal_evidence_comes_from_the_fence_s_own_lookup_after_the_close(
    *, left_behind: bool
) -> None:
    """The fence looks for leftovers itself, after the close, and removes what it finds.

    Before, ``remove(closure, None)`` took the caller's word that nothing was left.
    """
    with tempfile.TemporaryDirectory() as directory:
        fences = EffectFences(
            ReceiptStore(
                Project.open(Path(directory)).state.state_store_namespace("run"),
                threads=SimThreads(),
            )
        )
        closure = _closed_after_an_entry(fences)
        looked_closed: list[bool] = []
        removed: list[str] = []

        async def remove_it() -> None:
            removed.append("leftover")

        async def find() -> Callable[[], Awaitable[None]] | None:
            looked_closed.append(fences.is_closed(closure.key))
            return remove_it if left_behind else None

        evidence = await fences.remove(closure, find)
        assert looked_closed == [True]
        assert removed == (["leftover"] if left_behind else [])
        assert fences.cleared(closure, evidence).key == closure.key


async def test_a_failed_removal_is_no_evidence() -> None:
    """A removal that raises leaves the caller with an error, never with ``Removed``."""
    with tempfile.TemporaryDirectory() as directory:
        fences = EffectFences(
            ReceiptStore(
                Project.open(Path(directory)).state.state_store_namespace("run"),
                threads=SimThreads(),
            )
        )
        closure = _closed_after_an_entry(fences)

        async def fails() -> None:
            raise OSError("busy")

        async def find() -> Callable[[], Awaitable[None]] | None:
            return fails

        with pytest.raises(OSError, match="busy"):
            await fences.remove(closure, find)
