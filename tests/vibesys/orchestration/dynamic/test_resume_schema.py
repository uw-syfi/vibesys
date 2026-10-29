"""The dynamic strategy's checkpoints are pinned by readable snapshots, with their own versions.

``FlowState`` holds a flow's checkpoint as a JSON string, so the envelope's snapshot does not see
inside it. The root resumes from a ``DynamicResume`` and each workstream from a
``WorkstreamResume``, after a code change too; each has its own ``version``. Their snapshots fail
here until regenerated, and ``scripts/check_persisted_schema_compat.py`` holds a change against
the base branch's snapshot: under the same version, every checkpoint saved before it must still
decode.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.dynamic.search.api import DynamicResume, WorkstreamResume
from vs_core.api.testing import assert_schema_snapshot, schema_snapshot

if TYPE_CHECKING:
    from pydantic import BaseModel


@pytest.mark.parametrize(
    ("model", "golden"),
    [(DynamicResume, "dynamic_resume"), (WorkstreamResume, "workstream_resume")],
    ids=["dynamic", "workstream"],
)
def test_a_dynamic_checkpoint_shape_is_the_one_its_snapshot_pins(
    model: type[BaseModel], golden: str
) -> None:
    path = Path(__file__).parent / f"{golden}.persisted-schema.txt"
    version = model.model_fields["version"].default
    expected = schema_snapshot(model.__name__, (version,), model.model_json_schema())
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(expected, encoding="utf-8")
    assert_schema_snapshot(path.read_text(encoding="utf-8"), expected)
