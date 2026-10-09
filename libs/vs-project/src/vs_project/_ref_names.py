"""Git's reference-name rules (``git check-ref-format``), without starting Git.

libgit2 has its own, slightly different, validity check, so a library-backed
``GitRepository`` cannot use it and still answer exactly as the CLI does. This is
a port of ``check_refname_format`` from Git's ``refs.c``; the contract suite
compares it with ``git check-ref-format`` over generated names.
"""

from __future__ import annotations

_FORBIDDEN = frozenset(" ~^:?[\\\x7f") | frozenset(chr(code) for code in range(32))
_LOCK_SUFFIX = ".lock"


def is_valid_ref_name(name: str) -> bool:
    """Whether ``name`` is a legal full ref name with at least two components.

    A name starting with ``-`` is refused: ``git check-ref-format`` reads it as
    an option, and a ref name must never be mistakable for one.
    """
    if name == "@" or name.startswith("-"):
        return False
    if "/" not in name or name.endswith("."):
        return False
    return all(_is_valid_component(component) for component in name.split("/"))


def is_valid_branch_name(name: str) -> bool:
    """Whether ``name`` is a legal branch name: ``refs/heads/<name>`` is valid and it is not ``HEAD``."""
    return not name.startswith("-") and name != "HEAD" and is_valid_ref_name(f"refs/heads/{name}")


def _is_valid_component(component: str) -> bool:
    if not component or component.startswith(".") or component.endswith(_LOCK_SUFFIX):
        return False
    if ".." in component or "@{" in component or "*" in component:
        return False
    return not _FORBIDDEN & set(component)
