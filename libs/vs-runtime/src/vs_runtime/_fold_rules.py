"""The rules this process folds with: a digest of the source files that decide a fold.

``vs_core`` owns the rules (the reducers and the decision) and is pure, so it cannot read its own
source; this is the shell's part. A cache of a fold names the rules that made it (``Folded.encode``),
and a start under other rules folds the whole log (``start_fold``).

What decides a fold is what runs between the log's bytes and the state: the rules, the schema the
records decode with, and the reader that routes each record to a rule (``fold_log``). They are
derived from two roots and not listed: the package that owns the rules (every file of it), and the
reader's module. A package either root imports is read whole, and so are the packages those import;
a module of the reader's own package it imports is read alone, since the rest of that package is the
shell and decides nothing. Today that is ``vs_core``, ``vs_proto`` (the record schema and its
decoding) and ``vs_decode``, and ``vs_runtime/_saved_run_record.py``.

A change to any of those files changes the identity, which costs one fold of each log at the next
start and is never wrong. A build that ships no source for any of them has no identity (the empty
string), which matches no cache. Not covered: a library imported from outside the repository (the
lockfile pins it), and an import made by name at run time rather than by an import statement.
"""

from __future__ import annotations

import re
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING

from vs_core.api.recovery import rules_digest
from vs_runtime._saved_run_record import fold_log

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from importlib.resources.abc import Traversable

_NOT_RULES = frozenset({"testing", "__pycache__"})
"""Directories of a package that decide no fold: test support, compiled caches."""

RULES_PACKAGE = rules_digest.__module__.partition(".")[0]
"""The package that owns the rules: the one the digest of them comes from."""
READER = fold_log.__module__
"""The module of the reader the keeper folds a log with."""

_FIRST_PARTY = "vs_"
"""What every package of this repository's libraries is named by; others are pinned by the lockfile."""

_IMPORT = re.compile(
    rb"^[ \t]*(?:from|import)[ \t]+(" + _FIRST_PARTY.encode() + rb"[A-Za-z0-9_.]*)", re.MULTILINE
)
"""An import statement of a first-party module, at the start of a line (function-level ones too)."""

type Find = Callable[[str], Traversable | None]
"""The directory of a package's source, or None when the package is not installed."""


def installed(package: str) -> Traversable | None:
    """The source of an installed package, or None."""
    try:
        return resources.files(package)
    except ModuleNotFoundError:
        return None


def fold_sources(find: Find = installed) -> dict[str, bytes]:
    """The source files that decide a fold, by ``<package>/<path in it>``; empty when any is missing.

    A missing package, or a file the reader's module imports that is not there, is a build that
    ships no source for what it folds with, so it has no identity: empty, never a partial one.
    """
    found: dict[str, bytes] = {}
    whole: set[str] = set()
    modules: set[str] = set()
    pending: list[tuple[str, bool]] = [(RULES_PACKAGE, True), (READER, False)]
    while pending:
        name, is_package = pending.pop()
        top = name.partition(".")[0]
        if top in whole or (not is_package and name in modules):
            continue
        root = find(top)
        if root is None:
            return {}
        if is_package:
            whole.add(top)
            texts = dict(_package_files(root, f"{top}/"))
            if not texts:
                return {}
        else:
            modules.add(name)
            module = _module_file(root, name.split(".")[1:])
            if module is None:
                return {}
            texts = {f"{top}/{module[0]}": module[1]}
        found.update(texts)
        for imported in _imports(texts.values()):
            owner = imported.partition(".")[0]
            if owner != top:
                pending.append((owner, True))
            elif not is_package:
                pending.append((imported, False))
    return found


@cache
def fold_rules() -> str:
    """The identity of the rules this process folds with."""
    return rules_digest(fold_sources())


def _imports(files: Iterable[bytes]) -> set[str]:
    """The first-party modules the files import, by dotted name."""
    return {
        match.group(1).decode("ascii")
        for content in files
        if _FIRST_PARTY.encode() in content  # most files import nothing of ours: skip the scan
        for match in _IMPORT.finditer(content)
    }


def _package_files(directory: Traversable, prefix: str) -> Iterator[tuple[str, bytes]]:
    for entry in directory.iterdir():
        if entry.is_dir():
            if entry.name not in _NOT_RULES:
                yield from _package_files(entry, f"{prefix}{entry.name}/")
        elif entry.name.endswith(".py"):
            yield f"{prefix}{entry.name}", entry.read_bytes()


def _module_file(root: Traversable, parts: list[str]) -> tuple[str, bytes] | None:
    """The path under the package ``root`` and the source of the module ``parts`` names, or None.

    A module is a file ``a/b.py``, or a package ``a/b/__init__.py``; no parts is the package's own.
    """
    directory: Traversable | None = root
    for part in parts[:-1]:
        directory = _entry(directory, part, is_dir=True)
    wanted = [f"{parts[-1]}.py", f"{parts[-1]}/__init__.py"] if parts else ["__init__.py"]
    for relative in wanted:
        *folders, leaf = relative.split("/")
        where = directory
        for folder in folders:
            where = _entry(where, folder, is_dir=True)
        if (entry := _entry(where, leaf, is_dir=False)) is not None:
            return "/".join([*parts[:-1], *relative.split("/")]), entry.read_bytes()
    return None


def _entry(directory: Traversable | None, name: str, *, is_dir: bool) -> Traversable | None:
    """The child of ``directory`` called ``name`` that is a directory (or a file), or None."""
    if directory is None:
        return None
    return next((e for e in directory.iterdir() if e.name == name and e.is_dir() is is_dir), None)
