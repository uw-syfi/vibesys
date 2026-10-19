"""The clients name the synthesized task by a constant of their own; it must be the backend's."""

from __future__ import annotations

import re
from pathlib import Path

from vibesys.inputs._synthesis import SYNTHESIZED_TASK_NAME

_CLIENT_CONSTANT = (
    Path(__file__).resolve().parents[3] / "clients/libs/contracts/src/synthesized-task.ts"
)


def test_the_clients_synthesized_task_name_is_the_backends() -> None:
    declared = re.search(
        r"export const SYNTHESIZED_TASK_NAME = '([^']+)';", _CLIENT_CONSTANT.read_text()
    )
    assert declared is not None
    assert declared.group(1) == SYNTHESIZED_TASK_NAME
