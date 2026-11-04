"""Every Porcupine model in the shipped evaluators declares ``Hash``.

Without ``Hash``, Porcupine keys its memo on the set of linearized operations alone, so every
state reached with the same set shares one bucket and each lookup compares against all of
them. A heavily overlapped history then costs time quadratic in the states reached, and a
loaded CI runner exceeds the evaluator's check budget. A model's ``Hash`` must agree with its
``Equal``; each evaluator's own Go test (``model_hash_test.go``) checks that and counts the
comparisons. This check is structural: it fails when any ``porcupine.Model{...}`` literal under
``resources/evaluators`` omits ``Hash``, so a new model cannot ship without one.
"""

from __future__ import annotations

import re
from pathlib import Path

EVALUATORS = Path(__file__).resolve().parents[2] / "resources" / "evaluators"
MODEL_LITERAL = "porcupine.Model{"
FIELD = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*:(?!=)")


def _blank_strings_and_comments(source: str) -> str:
    """Replace the contents of Go strings, runes and comments with spaces, keeping offsets."""
    out = list(source)
    index = 0
    while index < len(source):
        two = source[index : index + 2]
        char = source[index]
        if two in ("//", "/*"):
            end = source.find("\n" if two == "//" else "*/", index)
            end = len(source) if end < 0 else end + (0 if two == "//" else 2)
        elif char in "\"'`":
            end = index + 1
            while end < len(source) and source[end] != char:
                end += 2 if source[end] == "\\" and char != "`" else 1
            end += 1
        else:
            index += 1
            continue
        for position in range(index, min(end, len(source))):
            if out[position] != "\n":
                out[position] = " "
        index = end
    return "".join(out)


def _top_level_fields(source: str, start: int) -> set[str]:
    """Field names set directly in the composite literal whose ``{`` is at ``start``."""
    depth = 0
    fields: set[str] = set()
    line_start = start
    for index in range(start, len(source)):
        char = source[index]
        if char in "{([":
            depth += 1
        elif char in "})]":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and char == "\n":
            line_start = index + 1
        if depth == 1 and char == ":":
            match = FIELD.search(source, line_start, index + 1)
            if match and source[line_start : match.start()].strip() == "":
                fields.add(match.group(1))
    return fields


def _models() -> list[tuple[Path, int, set[str]]]:
    found = []
    for path in sorted(EVALUATORS.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        source = _blank_strings_and_comments(path.read_text())
        for match in re.finditer(re.escape(MODEL_LITERAL), source):
            start = match.end() - 1
            line = source.count("\n", 0, start) + 1
            found.append((path, line, _top_level_fields(source, start)))
    return found


def test_every_porcupine_model_declares_hash() -> None:
    missing = [
        f"{path.relative_to(EVALUATORS.parents[1])}:{line}"
        for path, line, fields in _models()
        if "Hash" not in fields
    ]
    assert not missing, (
        "porcupine.Model without Hash makes the checker compare every state of a linearized "
        "set; add a Hash that agrees with Equal (and a case in the evaluator's "
        f"model_hash_test.go): {missing}"
    )


def test_the_scan_finds_the_models_of_every_evaluator_that_uses_porcupine() -> None:
    modelled = {path.relative_to(EVALUATORS).parts[0] for path, _, _ in _models()}
    users = {
        module.parent.name
        for module in EVALUATORS.glob("*/go.mod")
        if "anishathalye/porcupine" in module.read_text()
    }
    assert users, "no evaluator depends on porcupine; this check has nothing to scan"
    assert users <= modelled, (
        f"evaluators using porcupine with no model found: {sorted(users - modelled)}"
    )
