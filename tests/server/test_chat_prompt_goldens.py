"""Byte-exact goldens for the experiment-chat prompts.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as
a prompt diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.chat.prompts import (
    experiment_chat_continuation_prompt,
    experiment_chat_system_prompt,
)

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "chat_prompts"
_STATE_DIR = "/state/threads/thread-1"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("system", experiment_chat_system_prompt(_STATE_DIR)),
        ("continuation", experiment_chat_continuation_prompt(_STATE_DIR)),
    ],
)
def test_chat_prompt_matches_golden(name: str, text: str) -> None:
    snapshot = _SNAPSHOT_DIR / f"{name}.txt"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        snapshot.write_text(text, encoding="utf-8")
        return
    assert text == snapshot.read_text(encoding="utf-8")
