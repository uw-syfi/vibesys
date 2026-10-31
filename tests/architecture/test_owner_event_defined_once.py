"""The union of events an executor tells a resource's owner has one definition, in vs-core."""

import ast
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

_ROOT = Path(__file__).resolve().parents[2]
_CORE_TYPES = _ROOT / "libs/vs-core/src/vs_core/types/steps.py"


def _aliases_named(source: str, name: str) -> list[int]:
    """The lines of the type aliases *name* in *source*, written ``type`` or annotated."""
    return [
        node.lineno
        for node in walk_nodes(parse_source(source))
        if (isinstance(node, ast.TypeAlias) and node.name.id == name)
        or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        )
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("type OwnerEvent = A | B", [1]),
        ("OwnerEvent: TypeAlias = A | B", [1]),
        ("from vs_core.api.recovery import OwnerEvent", []),
        ("type Other = A | B", []),
    ],
)
def test_the_scan_sees_every_way_to_define_the_alias(source: str, expected: list[int]) -> None:
    assert _aliases_named(source, "OwnerEvent") == expected


def test_owner_event_is_defined_once_and_in_vs_core() -> None:
    found = [
        f"{path.relative_to(_ROOT)}:{line}"
        for root in (_ROOT / "libs", _ROOT / "src")
        for path in sorted(root.rglob("*.py"))
        if "tests" not in path.relative_to(root).parts and "_generated" not in path.parts
        for line in _aliases_named(path.read_text(encoding="utf-8"), "OwnerEvent")
    ]
    assert [item.split(":")[0] for item in found] == [str(_CORE_TYPES.relative_to(_ROOT))], found
