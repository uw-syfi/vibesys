"""Agent reply text becomes a model in exactly one place: ``parse_typed_response``.

Strict ``model_validate_json`` on ``result.text`` rejects a reply that has prose
or a stray brace beside its JSON object, which the recovering parser accepts. A
second route lets live turns and resumed turns disagree about the same text.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "src", *sorted((REPO / "libs").glob("*/src")))
PARSER = REPO / "libs/vs-agent/src/vs_agent/runner.py"
JSON_VALIDATORS = {"model_validate_json", "validate_json"}


def reads_reply_text(call: ast.Call) -> bool:
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    return any(
        isinstance(node, ast.Attribute) and node.attr == "text"
        for argument in arguments
        for node in ast.walk(argument)
    )


def validates_reply_text(source: str) -> list[int]:
    """Line numbers of JSON validation calls whose arguments read a ``.text``."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in JSON_VALIDATORS
        and reads_reply_text(node)
    ]


def test_the_checker_flags_strict_validation_of_reply_text() -> None:
    source = "a = M.model_validate_json(outcome.result.text)\nb = M.model_validate_json(raw)"

    assert validates_reply_text(source) == [1]


def test_no_module_validates_agent_reply_text_except_the_recovering_parser() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for root in SCAN_ROOTS
        for path in sorted(root.rglob("*.py"))
        if path != PARSER
        for line in validates_reply_text(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], "use Completed.parse or parse_typed_response for agent text"
