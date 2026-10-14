"""The effect fence: whichever of entering and closing comes first decides, across restarts."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import HostFence, HostId, RequestId
from vs_project.api import Project
from vs_runtime.api.core import (
    EffectFences,
    Entered,
    Fenced,
    FenceKey,
    FenceKind,
    MayHold,
    NothingHeld,
    ReceiptStore,
    StillCreatingError,
)
from vs_sim.api.testing import examples

_KEYS = (FenceKey(FenceKind.INVOCATION, "s/i1"), FenceKey(FenceKind.ATTEMPT_WORKSPACE, "a1:0"))
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
            fences, key, model = EffectFences(ReceiptStore(namespace)), _KEYS[index], models[index]
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
                    closure = fences.close(key, by=by, host=_host(epoch))
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
                            fences.cleared(closure)
                    else:
                        assert fences.cleared(closure).key == key
                        proven[index] = True


def test_only_the_fence_proves_that_nothing_is_held() -> None:
    """``ObservationFacts.never_held`` takes a NothingHeld, and only EffectFences makes one."""
    key = _KEYS[0]
    with pytest.raises(TypeError):
        NothingHeld(key, _mint=object())
    with pytest.raises(TypeError):
        MayHold(key, live=False, _mint=object())
