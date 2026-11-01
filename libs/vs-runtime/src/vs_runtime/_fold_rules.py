"""The rules this process folds with: a digest of the source files of ``vs_core``.

``vs_core`` owns the rules (the reducers and the decision) and is pure, so it cannot read its own source;
this is the shell's part. A cache of a fold names the rules that made it (``Folded.encode``), and a
start under other rules folds the whole log (``start_fold``).

A change to any file changes the identity, which costs one fold of each log at the next start and is
never wrong. A build that ships no source has no identity (the empty string), which matches no cache.
The fold also reads ``vs_proto`` and this package's record reader (``fold_log``), which the identity
does not cover: they decode and route records and hold no reducer.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from typing import TYPE_CHECKING

from vs_core.api.recovery import rules_digest

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

_NOT_RULES = frozenset({"testing", "__pycache__"})
"""Directories of ``vs_core`` that are no part of its rules: test support, compiled caches."""


def package_sources(root: Traversable | None = None) -> dict[str, bytes]:
    """The source files of ``vs_core`` that are rules, by path relative to the package."""
    found: dict[str, bytes] = {}
    _read(resources.files("vs_core") if root is None else root, "", found)
    return found


def _read(directory: Traversable, prefix: str, found: dict[str, bytes]) -> None:
    for entry in directory.iterdir():
        if entry.is_dir():
            if entry.name not in _NOT_RULES:
                _read(entry, f"{prefix}{entry.name}/", found)
        elif entry.name.endswith(".py"):
            found[f"{prefix}{entry.name}"] = entry.read_bytes()


@cache
def fold_rules() -> str:
    """The identity of the rules this process folds with."""
    return rules_digest(package_sources())
