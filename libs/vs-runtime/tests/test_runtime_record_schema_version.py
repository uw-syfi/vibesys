"""The saved runtime record's shape is pinned by a readable snapshot, with the versions a load checks.

Production saves a ``RuntimeRecord[FlowState]``: the envelope plus the runtime's own fields
(publications, cursors, owner events, decisions, wakes). A load refuses a record whose
``RUNTIME_SCHEMA_VERSION`` differs and migrates an older envelope, so the snapshot's version is
the pair. A shape change fails here until the snapshot is regenerated, and
``scripts/check_persisted_schema_compat.py`` then holds it against the base branch's snapshot:
under the same pair, every record saved before it must still decode.
"""

from __future__ import annotations

import os
from pathlib import Path

from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState
from vs_core.api.testing import assert_schema_snapshot, schema_snapshot
from vs_runtime.api.core import RUNTIME_SCHEMA_VERSION, RuntimeRecord

_GOLDEN = Path(__file__).parent / "runtime_record.persisted-schema.txt"


def test_the_saved_record_shape_is_the_one_its_snapshot_pins() -> None:
    schema = RuntimeRecord[FlowState].model_json_schema()
    version = (ENVELOPE_SCHEMA_VERSION, RUNTIME_SCHEMA_VERSION)
    expected = schema_snapshot("RuntimeRecord[FlowState]", version, schema)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        _GOLDEN.write_text(expected, encoding="utf-8")
    assert_schema_snapshot(_GOLDEN.read_text(encoding="utf-8"), expected)
