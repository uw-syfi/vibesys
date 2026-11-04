"""A golden of schema hashes passes only when its last line pins the current version's shape."""

from __future__ import annotations

import pytest

from vs_core.api.testing import assert_schema_pinned, schema_digest

_OLD: dict[str, object] = {"type": "object", "properties": {"a": {"type": "integer"}}}
_NEW: dict[str, object] = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
}


def _golden(*rows: tuple[int, dict[str, object]]) -> str:
    lines = ["# append-only", *(f"{version} {schema_digest(schema)}" for version, schema in rows)]
    return "\n".join(lines) + "\n"


def test_the_current_shape_pinned_last_for_the_current_version_passes() -> None:
    assert_schema_pinned(_golden((1, _OLD), (2, _NEW)), (2,), _NEW)


@pytest.mark.parametrize(
    ("rows", "version", "schema", "names"),
    [
        (((1, _OLD),), 1, _NEW, "bump the version"),
        (((1, _OLD),), 2, _NEW, "append the line '2 "),
        (((1, _OLD), (2, _NEW)), 1, _OLD, "does not end at the current version 1"),
        (((2, _OLD), (1, _NEW)), 1, _NEW, "not ascending"),
        (((1, _NEW), (2, _NEW)), 2, _NEW, "same shape"),
    ],
    ids=["shape-changed", "version-unpinned", "version-rolled-back", "unordered", "repeated"],
)
def test_a_golden_that_does_not_pin_the_current_shape_fails_naming_the_fix(
    rows: tuple[tuple[int, dict[str, object]], ...],
    version: int,
    schema: dict[str, object],
    names: str,
) -> None:
    with pytest.raises(AssertionError, match=names):
        assert_schema_pinned(_golden(*rows), (version,), schema)
