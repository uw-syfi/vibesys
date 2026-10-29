"""What an agent reads when a tool call is refused never tells it to commit.

The agent's workspace may have read-only Git metadata, and `submit_evaluation` snapshots the
working tree, new files included, so no refusal can send the agent to `git commit`.
"""

from __future__ import annotations

import re

import pytest

# test-isolation: the wording of the closed refusal set is private; the API carries it as text.
from vs_runtime._agent_tool_errors import ToolRefusal, render_tool_refusal

_COMMIT_ADVICE = re.compile(
    r"\b(git commit|commit (your|the|it|and|then|changes?)\b)", re.IGNORECASE
)


@pytest.mark.parametrize("reason", list(ToolRefusal), ids=lambda reason: reason.value)
def test_no_refusal_tells_the_agent_to_commit(reason: ToolRefusal) -> None:
    text = render_tool_refusal(reason, handles=("h1",))

    assert text
    assert _COMMIT_ADVICE.search(text) is None


def test_a_candidate_without_a_revision_is_pointed_at_the_working_tree_snapshot() -> None:
    text = render_tool_refusal(ToolRefusal.INVALID_PLAN)

    assert "snapshots your working tree, including new files" in text
