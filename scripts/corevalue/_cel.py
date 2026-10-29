"""Compile supported CEL rules to constructor-time pure Python checks."""

from __future__ import annotations

import ast
import re

from protobuf import (
    DescField,
    DescFieldValueEnum,
    DescFieldValueList,
    DescFieldValueMessage,
    DescMessage,
)

from scripts.corevalue._schema import (
    GenerationError,
    extension,
    is_union,
    members,
    options,
    type_of,
    union_members,
)

_MEMBER_PATH_LENGTH = 2


def cel(expression: str, message: DescMessage) -> str:
    """Compile the supported CEL subset to a checked Python expression.

    Proto enum comparisons use numbers; the value API uses persisted string spellings.
    Presence of a union member becomes an isinstance check on the closed union.
    """
    _source_calls(expression, message)
    _paths(expression, message)
    text = expression
    fields = {f.name: f for f in message.fields}
    text = re.sub(
        r"this\.(\w+)\.map\((\w+), \2\.([\w.]+)\)\.unique\(\)",
        r"(len(set(p.\3 for p in self.\1)) == len(self.\1))",
        text,
    )

    text = re.sub(
        r"(this\.[\w.]+)\.unique\(\)",
        r"(len(set(\1)) == len(\1))",
        text,
    )

    text = _decimals(text, message)
    text = re.sub(r"size\((this\.[\w.]+)\)", r"len(\1)", text)
    text = _macros(text, message)

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
    for path, field in _paths(expression, message).items():
        if isinstance(field.value, DescFieldValueEnum):
            table = {spelling: number for _, spelling, number in members(field.value.enum)}
            text = re.sub(rf"\b{re.escape(path)}\b", f"{table!r}.get({path}, 0)", text)
    text = text.replace("this.", "self.").replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", "not ", text).strip()
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise GenerationError(message.type_name, f"unsupported CEL {expression!r}") from error
    _validate_calls(tree, message, expression)
    _validate_names(tree, message)
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
        ast.ListComp,
        ast.Subscript,
        ast.BinOp,
        ast.Sub,
        ast.Add,
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
            named = isinstance(node.func, ast.Name) and node.func.id in {
                "len",
                "set",
                "isinstance",
                "all",
                "any",
                "tuple",
                "cel_integer",
                "str",
            }
            table = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Dict)
            )
            if not (named or table):
                raise GenerationError(message.type_name, f"unsupported CEL call in {expression!r}")


def _macros(text: str, message: DescMessage) -> str:
    """Lower list predicates and filters with their CEL lexical variable scope."""
    pattern = re.compile(r"(this\.[\w.]+)\.(all|filter|exists|map)\((\w+), ")
    while match := pattern.search(text):
        start = match.end()
        end = start
        depth = 1
        while end < len(text) and depth:
            depth += (text[end] == "(") - (text[end] == ")")
            end += 1
        if depth:
            raise GenerationError(message.type_name, "unclosed CEL list expression")
        body = _macros(text[start : end - 1], message)
        expression = _macro(match[2], match[3], match[1], body)
        text = text[: match.start()] + expression + text[end:]
    # size() may contain a lowered filter; it has CEL's ordinary list length semantics.
    return text.replace("size(", "len(").replace("string(", "str(")


def _decimals(expression: str, message: DescMessage) -> str:
    """CEL sees decimal strings; core stores unbounded integers with the same domain."""
    text = expression
    for path, field in _paths(expression, message).items():
        config = extension(field, options.ext_core_field)
        if config and config.integer:
            text = re.sub(rf"\b{re.escape(path)}\b", f"str({path})", text)
            text = text.replace(f"int(str({path}))", f"cel_integer({path})")
            text = text.replace(f"has(str({path}))", f"has({path})")
            text = text.replace(f'str({path}) == "0"', f"{path} == 0")
    if '"' in text or "'" in text:
        raise GenerationError(
            message.type_name, "CEL string literals require an explicit compiler binding"
        )
    return text


def _macro(kind: str, variable: str, collection: str, body: str) -> str:
    """CEL collection macros preserve order and compare using tuple value semantics."""
    if kind == "filter":
        return f"tuple({variable} for {variable} in {collection} if ({body}))"
    function = {"all": "all", "exists": "any", "map": "tuple"}[kind]
    return f"{function}(({body}) for {variable} in {collection})"


def _field_at(message: DescMessage, path: list[str]) -> DescField | None:
    """Resolve a scalar path against descriptors, including imported message fields."""
    field = next((f for f in message.fields if f.name == path[0]), None)
    if field is None or len(path) == 1:
        return field
    if isinstance(field.value, DescFieldValueMessage):
        return _field_at(field.value.message, path[1:])
    return None


def _paths(expression: str, message: DescMessage) -> dict[str, DescField]:
    """Resolve root and macro-variable paths before changing their representation."""
    bindings = {"this": message}
    for match in re.finditer(r"this\.(\w+)\.(?:all|filter|exists|map)\((\w+),", expression):
        field = _field_at(message, [match[1]])
        if field and isinstance(field.value, DescFieldValueList):
            bindings[match[2]] = field.value.element
    found = {}
    for match in re.finditer(r"\b(\w+)\.([\w.]+)", expression):
        if match[1] in bindings:
            owner = bindings[match[1]]
            parts = match[2].split(".")
            field = _field_at(owner, parts) if isinstance(owner, DescMessage) else None
            if field is None:
                receiver = (
                    _field_at(owner, parts[:-1])
                    if isinstance(owner, DescMessage) and len(parts) > 1
                    else None
                )
                if (
                    parts[-1] in {"all", "filter", "exists", "map", "unique"}
                    and receiver
                    and isinstance(receiver.value, DescFieldValueList)
                ):
                    continue
                raise GenerationError(message.type_name, f"unsupported CEL path {match[0]}")
            found[match[0]] = field
    return found


def _source_calls(expression: str, message: DescMessage) -> None:
    """Python helpers are emitted by the compiler, never accepted as CEL functions."""
    supported = {"has", "size", "string", "int", "all", "exists", "filter", "map", "unique"}
    for function in re.findall(r"\b(\w+)\s*\(", expression):
        if function not in supported:
            raise GenerationError(message.type_name, f"unsupported CEL function {function}")


def _validate_names(tree: ast.AST, message: DescMessage) -> None:
    """Every local name has lexical scope from its collection macro."""
    allowed = {"self", "len", "set", "isinstance", "all", "any", "tuple", "str", "cel_integer"}
    for field in message.fields:
        if isinstance(field.value, DescFieldValueMessage) and is_union(field.value.message):
            allowed.update(type_of(member) for member in union_members(field.value.message))
    _Names(allowed, message).visit(tree)


class _Names(ast.NodeVisitor):
    def __init__(self, allowed: set[str], message: DescMessage) -> None:
        self.allowed = allowed
        self.message = message

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id not in self.allowed:
            raise GenerationError(self.message.type_name, f"unbound CEL name {node.id}")

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        outer = self.allowed
        self.allowed = set(outer)
        for generator in node.generators:
            self.visit(generator.iter)
            self.allowed.add(generator.target.id)
            for condition in generator.ifs:
                self.visit(condition)
        self.visit(node.elt)
        self.allowed = outer
