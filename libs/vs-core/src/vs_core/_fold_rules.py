"""The identity of the rules that fold a log: a digest of the source of ``vs_core``.

A cache of a fold (``state.pb``) is the state those rules left after the log's first records. It is
valid for the log's bytes only under the rules that wrote it: a process whose reducers changed since
(a fix between a pause and the Resume) would fold the records after the cache with new rules over
state the old ones made, and get a state its own full fold would not. The cache therefore names the
rules it was folded by, and a start that finds other rules folds the whole log.

The rules are the reducers and the decision of ``vs_core``, so their identity is a digest of the
package's source files, read once. A change to any file changes it, which costs one fold of the log at
the next start and is never wrong. Where the sources cannot be read (a build that ships only compiled
modules) the identity is empty, and an empty identity matches no cache.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_PACKAGE = Path(__file__).parent
_NOT_RULES = frozenset({"testing", "__pycache__"})
"""Directories of the package that are no part of the rules: test support, compiled caches."""


def rules_digest(sources: Mapping[str, bytes]) -> str:
    """The identity of rules whose source files are ``sources``, by path relative to the package.

    It depends on every path and every byte, and not on the order the files were read in. No files
    is no identity: the empty string.
    """
    if not sources:
        return ""
    digest = hashlib.sha256()
    for path in sorted(sources):
        content = sources[path]
        digest.update(f"{path}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()


def package_sources(root: Path = _PACKAGE) -> dict[str, bytes]:
    """The source files of the package at ``root`` that are rules, by path relative to it."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*.py")
        if not _NOT_RULES & set(path.relative_to(root).parts)
    }


@cache
def fold_rules() -> str:
    """The identity of the rules this process folds with."""
    return rules_digest(package_sources())
