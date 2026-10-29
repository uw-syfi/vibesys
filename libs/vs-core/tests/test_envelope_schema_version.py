"""The persisted envelope's shape is pinned by a readable snapshot, one line per class and field.

A run is resumed from its stored ``RunEnvelope``. ``envelope.persisted-schema.txt`` is the JSON
schema of the envelope production persists, ``RunEnvelope[FlowState]`` (every strategy is a
flow and a flow's state is ``FlowState``), with the version it is written at. A shape change
fails here until the snapshot is regenerated, and ``scripts/check_persisted_schema_compat.py``
then holds the change against the base branch's snapshot: under the same version, every
envelope saved before it must still decode.
"""

from __future__ import annotations

import os
from pathlib import Path

from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState, RunEnvelope
from vs_core.api.testing import assert_schema_snapshot, schema_snapshot

_GOLDEN = Path(__file__).parent / "envelope.persisted-schema.txt"


def test_the_persisted_envelope_shape_is_the_one_its_snapshot_pins() -> None:
    schema = RunEnvelope[FlowState].model_json_schema()
    expected = schema_snapshot("RunEnvelope[FlowState]", (ENVELOPE_SCHEMA_VERSION,), schema)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        _GOLDEN.write_text(expected, encoding="utf-8")
    assert_schema_snapshot(_GOLDEN.read_text(encoding="utf-8"), expected)
