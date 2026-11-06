"""Hold the task manifest schemas to their versioning rules against the merge base.

A task schema is a message in proto/vibesys/cli/v1 with the option
``(vibesys.cli.v1.task_schema) = {major, minor}``; its schema is that message and
every message its fields reach. ``buf breaking`` (FILE) already refuses deletions,
renames, renumbering and type changes. This check adds what Buf cannot see:

* A schema of major version 0 is frozen once it is on the base: any change to its
  messages fails. A new format is a new message with the next minor version.
* From 1.0 every field carries ``(vibesys.cli.v1.task_schema_since_minor)``, at
  most the schema's minor version. A field the base lacks declares the head's
  minor version, and the head's minor version is above the base's.
* A changed protovalidate rule on a field or message the base has fails: a
  reader of the older minor version would read a file the rule now refuses, or
  refuse one it now reads.

scripts/check_task_schema.sh builds the Buf images of the merge base and of the
working tree and runs this module on them. The cases are exercised on fixture
schemas by scripts/tests/test_task_schema.sh.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PACKAGE = "vibesys.cli.v1"
_SCHEMA_OPTION = "[vibesys.cli.v1.task_schema]"
_SINCE_OPTION = "[vibesys.cli.v1.task_schema_since_minor]"
_FIELD_RULES = "[buf.validate.field]"
_MESSAGE_RULES = "[buf.validate.message]"
_RULE_CHANGE = "tightening needs a new major; loosening needs a reviewed waiver"

Message = dict[str, Any]


@dataclass(frozen=True)
class Schema:
    """A schema message, its version, and the messages it reaches by full name."""

    name: str
    major: int
    minor: int
    closure: dict[str, Message]


def messages(files: list[dict[str, Any]]) -> dict[str, Message]:
    """Every message of the CLI package, nested ones included, by full name."""
    out: dict[str, Message] = {}

    def walk(prefix: str, items: list[Message]) -> None:
        for m in items:
            name = f"{prefix}.{m['name']}"
            out[name] = m
            walk(name, m.get("nestedType", []))

    for f in files:
        if f.get("package") == _PACKAGE:
            walk("." + _PACKAGE, f.get("messageType", []))
    return out


def schemas(files: list[dict[str, Any]]) -> dict[str, Schema]:
    """The task schemas among the messages, by full name."""
    all_messages = messages(files)
    out: dict[str, Schema] = {}
    for name, m in all_messages.items():
        option = m.get("options", {}).get(_SCHEMA_OPTION)
        if option is None:
            continue
        closure: dict[str, Message] = {}
        pending = [name]
        while pending:
            current = pending.pop()
            if current in closure or current not in all_messages:
                continue
            closure[current] = all_messages[current]
            pending.extend(
                f["typeName"] for f in all_messages[current].get("field", []) if "typeName" in f
            )
        out[name] = Schema(name, int(option.get("major", 0)), int(option.get("minor", 0)), closure)
    return out


def _fields(m: Message) -> dict[int, Message]:
    return {int(f["number"]): f for f in m.get("field", [])}


def _since(f: Message) -> int | None:
    value = f.get("options", {}).get(_SINCE_OPTION)
    return None if value is None else int(value)


def _every_field_declares_its_minor(s: Schema) -> list[str]:
    out = []
    for name, m in sorted(s.closure.items()):
        for f in m.get("field", []):
            since = _since(f)
            if since is None:
                out.append(
                    f"{name}.{f['name']}: a field of task schema {s.major}.{s.minor} declares no task_schema_since_minor"
                )
            elif since > s.minor:
                out.append(
                    f"{name}.{f['name']}: task_schema_since_minor = {since} is past the schema's minor version {s.minor}"
                )
    return out


def _frozen(base: Schema, head: Schema) -> list[str]:
    return [
        f"{name}: task schema {base.major}.{base.minor} is frozen, and this changes it; "
        f"write the new format as a new message with the next minor version"
        for name in sorted(set(base.closure) | set(head.closure))
        if base.closure.get(name) != head.closure.get(name)
    ]


def _changed(name: str, old: Message, new: Message, minor: int) -> tuple[list[str], bool]:
    """What a message breaks against its base version, and whether it gained fields."""
    out = []
    if old.get("options", {}).get(_MESSAGE_RULES) != new.get("options", {}).get(_MESSAGE_RULES):
        out.append(f"{name}: its protovalidate rules changed: {_RULE_CHANGE}")
    old_fields = _fields(old)
    added = False
    for number, f in sorted(_fields(new).items()):
        before = old_fields.get(number)
        if before is None:
            added = True
            if _since(f) is not None and _since(f) != minor:
                out.append(
                    f"{name}.{f['name']}: a field added in this change declares task_schema_since_minor = {_since(f)}, "
                    f"not the schema's minor version {minor}"
                )
            continue
        if before.get("options", {}).get(_FIELD_RULES) != f.get("options", {}).get(_FIELD_RULES):
            out.append(f"{name}.{f['name']}: its protovalidate rules changed: {_RULE_CHANGE}")
        if _since(before) != _since(f):
            out.append(
                f"{name}.{f['name']}: task_schema_since_minor changed from {_since(before)} to {_since(f)}"
            )
    return out, added


def _additive(base: Schema, head: Schema) -> list[str]:
    out = _every_field_declares_its_minor(head)
    added = False
    for name, m in sorted(head.closure.items()):
        # A message the base lacks is all new: each of its fields is of this minor version.
        found, grew = _changed(name, base.closure.get(name, {"name": m["name"]}), m, head.minor)
        out.extend(found)
        added = added or grew
    if added and head.minor <= base.minor:
        out.append(
            f"{head.name}: fields were added, and the schema is still {head.major}.{head.minor}; raise its minor version to {base.minor + 1}"
        )
    if head.minor < base.minor:
        out.append(f"{head.name}: its minor version went down from {base.minor} to {head.minor}")
    return out


def violations(base_files: list[dict[str, Any]], head_files: list[dict[str, Any]]) -> list[str]:
    """What the head's task schemas break of the rules, against the base's."""
    base, head = schemas(base_files), schemas(head_files)
    out: list[str] = []
    for name, h in sorted(head.items()):
        b = base.get(name)
        if b is None:
            if h.major >= 1:
                out.extend(_every_field_declares_its_minor(h))
            continue
        if b.major != h.major:
            out.append(
                f"{name}: its major version changed from {b.major} to {h.major}; a new major version is a new message"
            )
        elif b.major == 0:
            if (b.minor, b.closure) != (h.minor, h.closure):
                out.extend(
                    _frozen(b, h)
                    or [f"{name}: task schema {b.major}.{b.minor} is frozen; its version changed"]
                )
        else:
            out.extend(_additive(b, h))
    out.extend(
        f"{name}: the task schema {b.major}.{b.minor} was removed"
        for name, b in sorted(base.items())
        if name not in head
    )
    return out


def main(argv: list[str]) -> int:
    """Compare two Buf images, in JSON, given by path: the base's, then the head's."""
    try:
        base_path, head_path = argv
    except ValueError:
        print("usage: python -m scripts.check_task_schema BASE.json HEAD.json", file=sys.stderr)
        return 2
    base, head = (json.loads(Path(p).read_text()).get("file", []) for p in (base_path, head_path))
    found = violations(base, head)
    for v in found:
        print(f"task schema: {v}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
