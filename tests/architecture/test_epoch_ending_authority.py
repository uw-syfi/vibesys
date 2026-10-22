"""Only vs-runlog defines the lifecycle arms that close an epoch."""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OWNER = Path("libs/vs-runlog/src/vs_runlog/_epoch.py")


def _duplicated_endings(source: str) -> list[int]:
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Set, ast.List, ast.Tuple))
        and {"parked", "ended"}
        <= {item.value for item in node.elts if isinstance(item, ast.Constant)}
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('state in {"parked", "ended"}', [1]),
        ('state in ("ended", "parked")', [1]),
        ('ENDINGS = frozenset(["ended", "parked"])', [1]),
        ("epoch_has_ended(epoch)", []),
        ('state == "ended"', []),
    ],
)
def test_ending_guard_recognizes_duplicates(source: str, expected: list[int]) -> None:
    assert _duplicated_endings(source) == expected


def test_only_runlog_defines_the_ended_epoch_set() -> None:
    violations = []
    for root in (_ROOT / "src", _ROOT / "libs"):
        for path in root.rglob("*.py"):
            relative = path.relative_to(_ROOT)
            if relative == _OWNER or {"tests", "_generated"} & set(relative.parts):
                continue
            violations.extend(
                f"{relative}:{line}: use vs_runlog.api.epoch_has_ended"
                for line in _duplicated_endings(path.read_text())
            )
    assert not violations, "\n".join(violations)
