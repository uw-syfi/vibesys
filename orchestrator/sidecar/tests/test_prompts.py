"""The lead's first prompt: rendered from its template, pinned by a golden, steered by the operator."""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from tests import helpers as h
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.prompts import render

GOLDENS = Path(__file__).parent / "goldens"


def lead_message(task: str, fanout: int, steering: str) -> orch.Message:
    """The lead's brief, as the orchestrator addresses it."""
    message = h.message()
    message.ClearField("text")
    message.lead_brief.CopyFrom(orch.LeadBrief(task=task, fanout_limit=fanout, steering=steering))
    return message


def test_the_lead_brief_with_steering_is_pinned() -> None:
    steering = "Prefer Codex workers for volume.\nStay under the budget I named."
    text = render(lead_message("kv-store", 4, steering))
    assert text == (GOLDENS / "lead_brief_steered.md").read_text().rstrip("\n")


def test_the_lead_brief_without_steering_is_pinned() -> None:
    text = render(lead_message("kv-store", 0, ""))
    assert text == (GOLDENS / "lead_brief_plain.md").read_text().rstrip("\n")


_TASKS = st.from_regex(r"[a-z][a-z0-9-]{0,30}", fullmatch=True)
_STEERING = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="{}%#"),
    min_size=1,
).filter(lambda text: text.strip() != "")


@given(task=_TASKS, fanout=st.integers(0, 64), steering=_STEERING)
def test_the_lead_brief_carries_the_task_fanout_and_steering_verbatim(
    task: str, fanout: int, steering: str
) -> None:
    text = render(lead_message(task, fanout, steering))
    assert f"`{task}`" in text
    assert f"--fanout {fanout}" in text
    assert steering.strip() in text


@given(task=_TASKS, fanout=st.integers(0, 64))
def test_the_lead_brief_without_steering_mentions_no_steering(task: str, fanout: int) -> None:
    assert "steering" not in render(lead_message(task, fanout, ""))
