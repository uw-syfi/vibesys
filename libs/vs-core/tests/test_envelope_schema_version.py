"""The persisted envelope's shape is pinned to its version, so a shape change cannot keep an old number.

A run is resumed from its stored ``RunEnvelope``; an envelope whose version matches but
whose shape differs fails with a generic validation error instead of the version check.
``envelope_schema_versions.txt`` lists, one line per version, the version and a hash of
the JSON schema of the envelope production persists: ``RunEnvelope[FlowState]``, since
every strategy is a flow and a flow's state is ``FlowState``. A shape change to the core
part or to a flow field without a new line fails here, and two branches that each take
the next version both append to the same last line, so they conflict in the file instead
of merging silently.
"""

from __future__ import annotations

from pathlib import Path

from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState, RunEnvelope
from vs_core.api.testing import assert_schema_pinned

_VERSIONS = Path(__file__).parent / "envelope_schema_versions.txt"


def test_the_persisted_envelope_shape_is_the_one_pinned_for_its_version() -> None:
    schema = RunEnvelope[FlowState].model_json_schema()
    assert_schema_pinned(_VERSIONS.read_text(encoding="utf-8"), (ENVELOPE_SCHEMA_VERSION,), schema)
