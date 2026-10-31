"""The recovery rule decides from a step's facts and never from which kind of step it is."""

import ast
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

_CORE = Path(__file__).resolve().parents[2] / "libs/vs-core/src/vs_core"
_RULE = ("_decide.py", "_actions.py", "_scope_order.py", "_step_attempts.py", "_step_log.py")
_NOT_STEP_KINDS = frozenset({"Finished", "Failed", "Cancelled", "Oneof", "Observation"})
"""What the rule may test an instance of: a step's result and its observation, and a log's record body."""


def _tested_classes(source: str) -> list[tuple[int, str]]:
    found = []
    for node in walk_nodes(parse_source(source)):
        if isinstance(node, ast.MatchClass) and isinstance(node.cls, ast.Name):
            found.append((node.lineno, node.cls.id))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
        ):
            found.append((node.lineno, ast.unparse(node.args[1])))
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("isinstance(request, DispatchTurn)", [(1, "DispatchTurn")]),
        ("isinstance(request, A | B)", [(1, "A | B")]),
        ("match request:\n    case DispatchTurn():\n        pass", [(2, "DispatchTurn")]),
        ("match result:\n    case Finished():\n        pass", [(2, "Finished")]),
        ("request_interrupt_policy(request)", []),
    ],
)
def test_the_scan_sees_every_way_to_test_a_kind(
    source: str, expected: list[tuple[int, str]]
) -> None:
    assert _tested_classes(source) == expected


def test_the_recovery_rule_tests_no_instance_of_a_step_kind() -> None:
    violations = [
        f"{name}:{line}: {tested}"
        for name in _RULE
        for line, tested in _tested_classes((_CORE / name).read_text())
        if tested not in _NOT_STEP_KINDS
    ]
    assert not violations, "\n".join(violations)
