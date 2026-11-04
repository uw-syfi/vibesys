"""The module groups the real-process start-up checks hold a process to name what they find.

A check that matches nothing cannot fail, so these drive the matching with generated module
names: a module under a group's package is reported under that group with its name in the
message, a lookalike name is not, and a group naming a module that does not exist is found.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from tests.support.light_imports import (
    DENIED_HEAVY,
    HEAVY,
    Layer,
    describe,
    is_heavy,
    missing_modules,
    offenders,
    under,
)

_PART = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
_ROOT = st.sampled_from(DENIED_HEAVY)
_SUBMODULE = st.lists(_PART, min_size=1, max_size=3).map(".".join)


def _layers_of(root: str) -> set[Layer]:
    """The layers that list ``root``: the oracle, which does not use the matching under test."""
    return {layer for layer, roots in HEAVY.items() if root in roots}


@given(root=_ROOT, submodule=_SUBMODULE)
def test_a_module_under_a_heavy_package_is_reported_under_its_layer(
    root: str, submodule: str
) -> None:
    name = f"{root}.{submodule}"
    found = offenders([name], HEAVY)

    assert set(found) == _layers_of(root)
    assert all(names == [name] for names in found.values())
    assert is_heavy(name)


@given(root=_ROOT, suffix=_PART)
def test_a_lookalike_name_is_not_under_the_package(root: str, suffix: str) -> None:
    """``vibesys_extras`` and ``mcp2`` are other packages than ``vibesys`` and ``mcp``."""
    lookalike = f"{root}{suffix}"

    assert offenders([lookalike, root[:-1]], {"layer": [root]}) == {}
    assert not under(lookalike, [root])


@given(modules=st.lists(st.one_of(_SUBMODULE, _ROOT.map(lambda root: f"{root}.x")), max_size=12))
def test_exactly_the_heavy_modules_are_reported_whatever_else_is_loaded(
    modules: list[str],
) -> None:
    found = offenders(modules, HEAVY)

    reported = {name for names in found.values() for name in names}
    assert reported == {name for name in modules if is_heavy(name)}
    assert all(layer in HEAVY for layer in found)


@given(root=_ROOT, submodule=_SUBMODULE)
def test_the_message_names_the_module_and_its_group(root: str, submodule: str) -> None:
    name = f"{root}.{submodule}"
    message = describe(offenders([name], HEAVY))

    assert name in message
    assert all(str(layer) in message for layer in _layers_of(root))


def test_every_module_a_heavy_layer_names_exists() -> None:
    """A layer naming a module that was renamed or removed bans nothing."""
    assert missing_modules(HEAVY) == []


def test_a_group_naming_a_missing_module_is_found() -> None:
    groups = {"gone": ("json", "no_such_package_here", "json.no_such_submodule")}

    assert missing_modules(groups) == ["json.no_such_submodule", "no_such_package_here"]
