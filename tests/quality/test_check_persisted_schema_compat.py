"""The persisted-shape check fails a snapshot change that some saved document may not survive.

Snapshots are written from small schemas through the vs-core snapshot API, so the check is
driven exactly as CI drives it, minus the Git listing (``problems`` takes the texts).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from scripts.check_persisted_schema_compat import SUFFIX, problems

from vs_core.api.testing import schema_snapshot

_PATH = f"libs/x/tests/record{SUFFIX}"


def _shape(fields: dict[str, dict[str, object]], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": fields,
        **({"required": required} if required else {}),
    }


_BASE = _shape({"count": {"type": "integer", "maximum": 5}}, ["count"])


@given(version=st.integers(1, 50), bound=st.integers(5, 9))
def test_a_looser_bound_and_a_defaulted_field_pass_under_the_same_version(
    version: int, bound: int
) -> None:
    head = _shape(
        {"count": {"type": "integer", "maximum": bound}, "note": {"type": "string", "default": ""}},
        ["count"],
    )
    base_text = schema_snapshot("Record", (version,), _BASE)
    assert problems({_PATH: base_text}, {_PATH: schema_snapshot("Record", (version,), head)}) == []


@given(version=st.integers(1, 50), bound=st.integers(0, 4))
def test_a_tighter_bound_fails_naming_the_snapshot_shape_and_field_unless_the_version_rises(
    version: int, bound: int
) -> None:
    head = _shape({"count": {"type": "integer", "maximum": bound}}, ["count"])
    base_text = schema_snapshot("Record", (version,), _BASE)
    kept = problems({_PATH: base_text}, {_PATH: schema_snapshot("Record", (version,), head)})
    assert len(kept) == 1
    assert kept[0].startswith(f"{_PATH}: Record count: maximum tightened")
    raised = schema_snapshot("Record", (version + 1,), head)
    assert problems({_PATH: base_text}, {_PATH: raised}) == []


def test_a_new_snapshot_passes_and_a_deleted_one_fails() -> None:
    text = schema_snapshot("Record", (1,), _BASE)
    assert problems({}, {_PATH: text}) == []
    assert problems({_PATH: text}, {}) == [
        f"{_PATH}: the snapshot was deleted; a persisted shape keeps its snapshot"
    ]
