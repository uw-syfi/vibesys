"""The saved runtime record's shape is pinned to the versions a load checks.

Production saves a ``RuntimeRecord[FlowState]``: the envelope plus the runtime's own fields
(publications, cursors, owner events, decisions, wakes). A load refuses a record whose
``RUNTIME_SCHEMA_VERSION`` or whose envelope's ``ENVELOPE_SCHEMA_VERSION`` differs, so the
whole record's shape is pinned to the pair: a change anywhere in it, the core events it
queues included, without a new version of either fails here.
"""

from __future__ import annotations

from pathlib import Path

from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState
from vs_core.api.testing import assert_schema_pinned
from vs_runtime.api.core import RUNTIME_SCHEMA_VERSION, RuntimeRecord

_VERSIONS = Path(__file__).parent / "runtime_record_schema_versions.txt"


def test_the_saved_record_shape_is_the_one_pinned_for_its_versions() -> None:
    schema = RuntimeRecord[FlowState].model_json_schema()
    assert_schema_pinned(
        _VERSIONS.read_text(encoding="utf-8"),
        (ENVELOPE_SCHEMA_VERSION, RUNTIME_SCHEMA_VERSION),
        schema,
    )
