"""Every Go evaluator package resolves its dependencies the way a run does.

VibeSys copies an evaluator package away from this checkout before running it,
so a `replace` directive that points at a local path (the usual way to build
against an unreleased `sdk/vs-evaluator/vseval`) no longer resolves there. CI
would build against the local SDK while real runs fail or build a different
version. Consumers require a published SDK version instead; `sdk/README.md`
says how to co-develop through an uncommitted Go workspace.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

REPO_ROOT = Path(__file__).parents[2]
EVALUATOR_MODULES = sorted((REPO_ROOT / "resources" / "evaluators").glob("*/go.mod"))

# One replacement inside a `replace ( ... )` block or after a single-line `replace`:
# `<module> [version] => <target> [version]`.
_REPLACEMENT = re.compile(r"^\s*(?:replace\s+)?\S+(?:\s+\S+)?\s*=>\s*(?P<target>\S+)")


def local_replacements(go_mod: str) -> list[str]:
    """The `replace` targets in ``go_mod`` that are filesystem paths, not module versions."""
    targets: list[str] = []
    in_block = False
    for raw in go_mod.splitlines():
        line = raw.split("//", 1)[0].strip()
        if in_block and line == ")":
            in_block = False
            continue
        if line.startswith("replace") and line.removeprefix("replace").strip() == "(":
            in_block = True
            continue
        if not (in_block or line.startswith("replace ")):
            continue
        match = _REPLACEMENT.match(line)
        if match is not None and match["target"].startswith(("./", "../", "/")):
            targets.append(match["target"])
    return targets


def test_evaluator_modules_exist() -> None:
    assert EVALUATOR_MODULES, "no Go evaluator module found under resources/evaluators/"


@pytest.mark.parametrize("go_mod", EVALUATOR_MODULES, ids=lambda path: path.parent.name)
def test_evaluator_module_has_no_local_replace(go_mod: Path) -> None:
    targets = local_replacements(go_mod.read_text(encoding="utf-8"))
    assert not targets, (
        f"{go_mod.relative_to(REPO_ROOT)}: `replace` points at local path(s) {targets}, which "
        "do not exist where VibeSys runs the evaluator. Require a published version and "
        "co-develop through an uncommitted go.work (see sdk/README.md)."
    )


_SEGMENT = st.from_regex(r"[a-z][a-z0-9-]{0,8}", fullmatch=True)
_MODULE = st.lists(_SEGMENT, min_size=1, max_size=4).map("/".join)
_VERSION = st.from_regex(r"v[0-9]\.[0-9]{1,2}\.[0-9]{1,2}", fullmatch=True)
_LOCAL = st.tuples(st.sampled_from(["./", "../", "/"]), _MODULE).map("".join)


@given(module=_MODULE, local=_LOCAL, block=st.booleans(), pinned=st.booleans(), version=_VERSION)
def test_local_replace_is_found_in_either_form(
    module: str, local: str, *, block: bool, pinned: bool, version: str
) -> None:
    source = f"{module} {version}" if pinned else module
    directive = f"replace (\n\t{source} => {local}\n)" if block else f"replace {source} => {local}"
    go_mod = f"module example.com/x\n\ngo 1.21\n\n{directive}\n"
    assert local_replacements(go_mod) == [local]


@given(module=_MODULE, target=_MODULE, version=_VERSION, block=st.booleans())
def test_module_replace_and_plain_requires_are_allowed(
    module: str, target: str, version: str, *, block: bool
) -> None:
    replacement = f"{module} => example.com/{target} {version}"
    directive = f"replace (\n\t{replacement}\n)" if block else f"replace {replacement}"
    go_mod = f"module example.com/x\n\nrequire {module} {version}\n\n{directive}\n"
    assert local_replacements(go_mod) == []
