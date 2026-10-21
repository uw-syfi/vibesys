"""Compile supported CEL rules to constructor-time pure Python checks."""

from __future__ import annotations

import ast
import re

from protobuf import DescFieldValueEnum, DescFieldValueMessage, DescMessage

from scripts.corevalue._schema import GenerationError, is_union, members, type_of, union_members

_MEMBER_PATH_LENGTH = 2


def cel(expression: str, message: DescMessage) -> str:
    """Compile the supported CEL subset to a checked Python expression.

    Proto enum comparisons use numbers; the value API uses persisted string spellings.
    Presence of a union member becomes an isinstance check on the closed union.
    """
    if '"' in expression or "'" in expression:
        raise GenerationError(
            message.type_name, "CEL string literals require an explicit compiler binding"
        )
    text = expression
    fields = {f.name: f for f in message.fields}
    text = re.sub(
        r"this\.(\w+)\.map\((\w+), \2\.([\w.]+)\)\.unique\(\)",
        r"(len(set(p.\3 for p in self.\1)) == len(self.\1))",
        text,
    )

    def present(match: re.Match[str]) -> str:
        """Present."""
        path = match[1].split(".")
        field = fields[path[0]]
        if len(path) == 1:
            return f"(self.{path[0]} is not None)"
        if (
            len(path) == _MEMBER_PATH_LENGTH
            and isinstance(field.value, DescFieldValueMessage)
            and is_union(field.value.message)
        ):
            member = next(f for f in union_members(field.value.message) if f.name == path[1])
            return f"isinstance(self.{path[0]}, {type_of(member)})"
        raise GenerationError(message.type_name, f"unsupported presence path {match[1]}")

    text = re.sub(r"has\(this\.([\w.]+)\)", present, text)
    for field in fields.values():
        if isinstance(field.value, DescFieldValueEnum):
            table = {spelling: number for _, spelling, number in members(field.value.enum)}
            text = re.sub(rf"\bthis\.{field.name}\b", f"{table!r}.get(self.{field.name}, 0)", text)
    text = text.replace("this.", "self.").replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", "not ", text).strip()
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise GenerationError(message.type_name, f"unsupported CEL {expression!r}") from error
    _validate_calls(tree, message, expression)
    allowed = (
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.GtE,
        ast.Gt,
        ast.LtE,
        ast.Lt,
        ast.In,
        ast.NotIn,
        ast.IsNot,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Attribute,
        ast.Dict,
        ast.List,
        ast.Call,
        ast.GeneratorExp,
        ast.comprehension,
        ast.Store,
    )
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise GenerationError(message.type_name, f"unsupported CEL {expression!r}")
    return text


def _validate_calls(tree: ast.AST, message: DescMessage, expression: str) -> None:
    """Only the calls introduced by the compiler may enter constructor code."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            named = isinstance(node.func, ast.Name) and node.func.id in {"len", "set", "isinstance"}
            table = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Dict)
            )
            if not (named or table):
                raise GenerationError(message.type_name, f"unsupported CEL call in {expression!r}")
