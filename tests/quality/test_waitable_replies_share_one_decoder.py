"""A strategy's agent reply that can wait for an evaluation is a ``WaitableReply``.

The shared protocol prompt tells every role to end a wait with the object-rooted form
``{"reply": {"kind": "waiting_for_evaluation", ...}}``. ``vs_core.api.WaitableReply`` is the one
model that accepts that form and the older bare form. A strategy that builds its own model or
adapter around the bare ``Waitable`` union rejects the enveloped form, which discards the work of
the role that answered with it. So the bare union may be used only inside ``WaitableReply``.
"""

import ast
from pathlib import Path

from tests.support.parsed_source import parse_source, walk_nodes

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "src/vibesys/orchestration", REPO / "src/vibesys/run")
BARE_UNION = "Waitable"
DECODERS = {"TypeAdapter", "RootModel"}


def _names(node: ast.AST) -> set[str]:
    return {item.id for item in walk_nodes(node) if isinstance(item, ast.Name)}


def bare_union_decoders(source: str) -> list[int]:
    """Line numbers of a decoder or model base built directly on the bare ``Waitable`` union."""
    lines: list[int] = []
    for node in walk_nodes(parse_source(source)):
        if isinstance(node, ast.ClassDef):
            lines += [
                base.lineno
                for base in node.bases
                if isinstance(base, ast.Subscript)
                and _is_decoder(base.value)
                and BARE_UNION in _names(base.slice)
            ]
        elif (
            isinstance(node, ast.Call)
            and _is_decoder(node.func)
            and any(BARE_UNION in _names(argument) for argument in node.args)
        ):
            lines.append(node.lineno)
    return lines


def _is_decoder(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in DECODERS


def test_the_checker_flags_decoders_built_on_the_bare_union() -> None:
    assert bare_union_decoders("A = TypeAdapter(Waitable[R])") == [1]
    assert bare_union_decoders("class M(RootModel[Waitable[R]]):\n    pass") == [1]
    assert bare_union_decoders("A = WaitableReply[R]\nclass M(WaitableReply[R]):\n    pass") == []
    assert bare_union_decoders("type Reply = Waitable[R]") == []


def test_no_strategy_decodes_a_wait_without_waitable_reply() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for root in SCAN_ROOTS
        for path in sorted(root.rglob("*.py"))
        for line in bare_union_decoders(path.read_text())
    ]

    assert offenders == [], "use vs_core.api.WaitableReply, not a decoder on the bare Waitable"
