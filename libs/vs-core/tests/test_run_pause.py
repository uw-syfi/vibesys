"""A paused run's controls say who holds it and whether the operator asked for a capacity wait."""

from __future__ import annotations

import json
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

type _Kind = Literal["operator", "quota", "now", "fallback", "capacity"]
_KINDS: tuple[_Kind, ...] = ("quota", "now", "fallback", "capacity")


def _control(kind: _Kind, index: int) -> core.ControlInput:
    identity = core.ControlId(root=f"op-{index}")
    match kind:
        case "operator" | "quota":
            return core.ControlInput(
                control_id=identity, action="pause", owner=core.PauseOwner(kind)
            )
        case "now" | "fallback":
            return core.ControlInput(
                control_id=identity, action="resume", when="now", fallback=kind == "fallback"
            )
        case "capacity":
            return core.ControlInput(control_id=identity, action="resume", when="capacity_returns")


def _expected(pause: core.RunPause | None, kind: _Kind) -> core.RunPause | None:
    """The oracle, stated per control rather than as a fold over the record."""
    match kind:
        case "operator":
            return core.RunPause(owner=core.PauseOwner.OPERATOR)
        case "quota":
            return pause if pause is not None else core.RunPause(owner=core.PauseOwner.QUOTA)
        case "now" | "fallback":
            return None
        case "capacity":
            assert pause is not None
            return core.RunPause(owner=pause.owner, capacity_wait=True)


@given(st.lists(st.sampled_from(_KINDS), max_size=20))
def test_the_pause_follows_its_controls_and_a_capacity_wait_needs_a_paused_run(
    kinds: list[_Kind],
) -> None:
    state = core.initial_state()
    pause: core.RunPause | None = None
    for index, kind in enumerate(kinds):
        event = core.RunControlEvent(control=_control(kind, index), now_at=float(index))
        if kind == "capacity" and pause is None:
            with pytest.raises(core.ContractError, match="capacity"):
                core.step(state, event)
            assert core.control_refusal(state, event.control) is not None
            continue
        assert core.control_refusal(state, event.control) is None
        state = core.step(state, event).state
        pause = _expected(pause, kind)
        assert core.run_pause(state.run) == pause
        assert (state.run.status == core.RunStatus.PAUSED) == (pause is not None)


@pytest.mark.parametrize(
    ("recorded", "resent"),
    [
        pytest.param("now", "fallback", id="fallback"),
        pytest.param("now", "capacity", id="when"),
        pytest.param("operator", "quota", id="owner"),
    ],
)
def test_a_control_resent_under_its_id_with_another_effect_is_a_conflict(
    recorded: _Kind, resent: _Kind
) -> None:
    state = core.initial_state()
    pause = core.RunControlEvent(control=_control("quota", 0), now_at=0.0)
    state = core.step(state, pause).state
    state = core.step(state, core.RunControlEvent(control=_control(recorded, 1), now_at=1.0)).state

    with pytest.raises(core.ContractError, match="conflict"):
        core.step(state, core.RunControlEvent(control=_control(resent, 1), now_at=2.0))


@pytest.mark.parametrize(
    ("action", "fields", "key"),
    [
        pytest.param("pause", {}, "owner", id="pause-without-owner"),
        pytest.param("resume", {}, "when", id="resume-without-when"),
        pytest.param("steer", {"owner": "operator"}, "owner", id="owner-on-steer"),
        pytest.param("pause", {"owner": "quota", "when": "now"}, "when", id="when-on-pause"),
        pytest.param(
            "resume", {"when": "capacity_returns", "fallback": True}, "fallback", id="fallback-wait"
        ),
    ],
)
def test_a_control_carries_exactly_the_fields_its_action_has(
    action: str, fields: dict[str, object], key: str
) -> None:
    with pytest.raises(ValueError, match=key):
        core.ControlInput.model_validate_json(
            json.dumps({"control_id": {"root": "c"}, "action": action, **fields})
        )
